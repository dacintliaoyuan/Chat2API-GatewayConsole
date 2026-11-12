# -*- coding: utf-8 -*-
"""E19-1-03: Responses/Messages protocol helpers live in ``shared``.

The Responses and Messages request/response/stream translations are pure protocol
code consumed by both applications, so they belong to ``shared``.  These tests pin
the public conversion behavior (admission, request mapping, non-stream response
mapping and SSE translation) and prove that importing the shared module does not
load the share gateway host.

Expected values are written from the wire contracts (OpenAI Responses, OpenAI Chat
Completions and Anthropic Messages), not recomputed from the implementation.
"""

import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULE_PATH = AI_GATEWAY_ROOT / "shared" / "responses_protocol.py"
SHARE_GATEWAY_PATH = AI_GATEWAY_ROOT / "chat2api" / "share_gateway.py"

sys.path.insert(0, str(AI_GATEWAY_ROOT))

from shared import responses_protocol as protocol  # noqa: E402


CHAT_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"content":"hello"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    b'"id":"call_1","function":{"name":"lookup","arguments":"{\\"q\\":"}}]},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    b'"function":{"arguments":"1}"}}]},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":5,'
    b'"total_tokens":15}}\n\n'
    b'data: [DONE]\n\n'
)

ANTHROPIC_SSE = (
    b'event: message_start\n'
    b'data: {"type":"message_start","message":{"id":"msg_1",'
    b'"type":"message","role":"assistant","model":"m","content":[],'
    b'"stop_reason":null,"stop_sequence":null,'
    b'"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"hello"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b'event: content_block_start\n'
    b'data: {"type":"content_block_start","index":1,'
    b'"content_block":{"type":"tool_use","id":"toolu_1",'
    b'"name":"lookup","input":{}}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"{\\"q\\":"}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type":"content_block_delta","index":1,'
    b'"delta":{"type":"input_json_delta","partial_json":"1}"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type":"content_block_stop","index":1}\n\n'
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    b'"stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)

CLEAN_IMPORT_PROBE = """
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import shared.responses_protocol

print(json.dumps(sorted(sys.modules)))
"""


def _event_names(events):
    return [name for name, _payload in events]


def _event_payload(events, name):
    matches = [payload for event_name, payload in events if event_name == name]
    return matches[-1] if matches else None


class ValidResponsesRequestTests(unittest.TestCase):
    """Admission of the narrow Responses subset the share translates."""

    def test_minimal_request_is_accepted(self):
        self.assertTrue(protocol.valid_responses_request(
            {"model": "fake-um-model", "input": "hello"}))

    def test_harness_defaults_are_accepted(self):
        body = {
            "model": "fake-um-model",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
            "instructions": "be brief",
            "stream": True,
            "max_output_tokens": 64,
            "temperature": 0.2,
            "top_p": 0.9,
            "tools": [{"type": "function", "name": "lookup",
                       "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "lookup"},
            "store": False,
            "reasoning": {"effort": "high"},
            "include": ["reasoning.encrypted_content"],
        }
        self.assertTrue(protocol.valid_responses_request(body))

    def test_unknown_field_is_rejected(self):
        self.assertFalse(protocol.valid_responses_request({
            "model": "fake-um-model", "input": "hello", "server_tools": []}))

    def test_missing_or_blank_model_is_rejected(self):
        for body in ({}, {"input": "hello"},
                     {"model": "", "input": "hello"},
                     {"model": 7, "input": "hello"}):
            with self.subTest(body=body):
                self.assertFalse(protocol.valid_responses_request(body))

    def test_empty_or_wrong_input_is_rejected(self):
        for value in (None, "", [], {}, 3):
            with self.subTest(value=value):
                self.assertFalse(protocol.valid_responses_request({
                    "model": "fake-um-model", "input": value}))

    def test_stream_must_be_boolean_when_present(self):
        self.assertFalse(protocol.valid_responses_request({
            "model": "fake-um-model", "input": "hello", "stream": "yes"}))

    def test_store_true_is_rejected(self):
        self.assertFalse(protocol.valid_responses_request({
            "model": "fake-um-model", "input": "hello", "store": True}))

    def test_invalid_numeric_limits_are_rejected(self):
        for value in (0, -1, True, "64", 1.5):
            with self.subTest(value=value):
                self.assertFalse(protocol.valid_responses_request({
                    "model": "fake-um-model", "input": "hello",
                    "max_output_tokens": value}))
        for name in ("temperature", "top_p"):
            for value in (True, "0.5", []):
                with self.subTest(name=name, value=value):
                    self.assertFalse(protocol.valid_responses_request({
                        "model": "fake-um-model", "input": "hello",
                        name: value}))

    def test_malformed_tools_and_include_are_rejected(self):
        for tools in ("lookup", [{"type": ""}], [{"type": "function"}]):
            with self.subTest(tools=tools):
                self.assertFalse(protocol.valid_responses_request({
                    "model": "fake-um-model", "input": "hello",
                    "tools": tools}))
        for include in ("reasoning.encrypted_content", [1]):
            with self.subTest(include=include):
                self.assertFalse(protocol.valid_responses_request({
                    "model": "fake-um-model", "input": "hello",
                    "include": include}))

    def test_input_that_translates_to_no_messages_is_rejected(self):
        self.assertFalse(protocol.valid_responses_request({
            "model": "fake-um-model",
            "input": [{"type": "reasoning", "id": "rs_1"}]}))

    def test_builtin_tool_is_accepted_but_has_no_chat_shape(self):
        body = {"model": "fake-um-model", "input": "hello",
                "tools": [{"type": "web_search"}]}
        self.assertTrue(protocol.valid_responses_request(body))
        translated = protocol.responses_to_chat_request(body)
        self.assertNotIn("tools", translated)


class ResponsesToChatRequestTests(unittest.TestCase):
    """Responses request -> Chat Completions request mapping."""

    def test_full_request_maps_onto_chat_fields(self):
        body = {
            "model": "fake-um-model",
            "instructions": "be brief",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "hi"}]}],
            "stream": True,
            "max_output_tokens": 64,
            "temperature": 0.2,
            "top_p": 0.9,
            "tools": [{"type": "function", "name": "lookup",
                       "description": "find a row",
                       "parameters": {"type": "object"}, "strict": True}],
            "tool_choice": {"type": "function", "name": "lookup"},
            "parallel_tool_calls": False,
            "reasoning": {"effort": "high"},
        }
        translated = protocol.responses_to_chat_request(body)
        self.assertEqual(translated, {
            "model": "fake-um-model",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": 64,
            "temperature": 0.2,
            "top_p": 0.9,
            "tools": [{"type": "function", "function": {
                "name": "lookup", "description": "find a row",
                "parameters": {"type": "object"}, "strict": True}}],
            "tool_choice": {"type": "function", "function": {
                "name": "lookup"}},
            "parallel_tool_calls": False,
            "reasoning_effort": "high",
        })

    def test_consecutive_function_calls_share_one_assistant_message(self):
        messages = protocol.responses_to_chat_messages({
            "input": [
                {"type": "function_call", "call_id": "call_1",
                 "name": "lookup", "arguments": "{\"q\": 1}"},
                {"type": "function_call", "call_id": "call_2",
                 "name": "lookup", "arguments": "{\"q\": 2}"},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "first"},
                {"type": "function_call_output", "call_id": "call_2",
                 "output": [{"type": "input_text", "text": "second"}]},
            ]})
        self.assertEqual(len(messages), 3)
        assistant = messages[0]
        self.assertEqual(assistant["role"], "assistant")
        self.assertIsNone(assistant["content"])
        self.assertEqual(
            [call["id"] for call in assistant["tool_calls"]],
            ["call_1", "call_2"])
        self.assertEqual(messages[1], {
            "role": "tool", "tool_call_id": "call_1", "content": "first"})
        self.assertEqual(messages[2], {
            "role": "tool", "tool_call_id": "call_2", "content": "second"})

    def test_developer_role_becomes_system(self):
        messages = protocol.responses_to_chat_messages({
            "input": [{"role": "developer", "content": "rules"}]})
        self.assertEqual(messages, [{"role": "system", "content": "rules"}])

    def test_image_content_maps_to_chat_image_parts(self):
        messages = protocol.responses_to_chat_messages({
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "look"},
                {"type": "input_image", "image_url": "https://example.test/i.png"},
            ]}]})
        self.assertEqual(messages, [{"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image_url",
             "image_url": {"url": "https://example.test/i.png"}},
        ]}])

    def test_string_tool_choice_passes_through(self):
        translated = protocol.responses_to_chat_request({
            "model": "m", "input": "hi", "tool_choice": "required"})
        self.assertEqual(translated["tool_choice"], "required")


class ResponsesToMessagesRequestTests(unittest.TestCase):
    """Responses request -> Anthropic Messages request mapping."""

    def test_tool_choice_mapping(self):
        cases = (
            ("auto", {"type": "auto"}),
            ("none", {"type": "none"}),
            ("required", {"type": "any"}),
            ({"type": "function", "name": "lookup"},
             {"type": "tool", "name": "lookup"}),
            ({"type": "any"}, {"type": "any"}),
            ({"type": "function"}, None),
            ("bogus", None),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    protocol.responses_to_messages_tool_choice(value), expected)

    def test_tool_result_images_require_messages(self):
        text_only = {"input": [{"type": "function_call_output",
                                "call_id": "call_1", "output": "done"}]}
        image_result = {"input": [{"type": "function_call_output",
                                  "call_id": "call_1", "output": [
                                      {"type": "input_image",
                                       "image_url": "https://example.test/i.png"}]}]}
        message_image = {"input": [{"role": "user", "content": [
            {"type": "input_image",
             "image_url": "https://example.test/i.png"}]}]}
        self.assertFalse(protocol.responses_requires_messages(text_only))
        self.assertTrue(protocol.responses_requires_messages(image_result))
        self.assertFalse(protocol.responses_requires_messages(message_image))

    def test_full_request_keeps_tool_result_images(self):
        body = {
            "model": "fake-um-model",
            "instructions": "be brief",
            "input": [
                {"role": "system", "content": [
                    {"type": "input_text", "text": "rules"}]},
                {"role": "user", "content": [
                    {"type": "input_text", "text": "hi"},
                    {"type": "input_image",
                     "image_url": {"url": "data:image/png;base64,AAAA"}}]},
                {"type": "function_call", "call_id": "call_1",
                 "name": "lookup", "arguments": "{\"q\": 1}"},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": [
                     {"type": "input_text", "text": "result"},
                     {"type": "input_image",
                      "image_url": "https://example.test/i.png"}]},
            ],
            "max_output_tokens": 100,
            "stream": True,
            "temperature": 0.2,
            "top_p": 0.9,
            "tools": [{"type": "function", "name": "lookup",
                       "description": "find a row",
                       "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "lookup"},
        }
        translated = protocol.responses_to_messages_request(body)
        self.assertEqual(translated, {
            "model": "fake-um-model",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png",
                        "data": "AAAA"}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_1", "name": "lookup",
                     "input": {"q": 1}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_1",
                     "content": [
                         {"type": "text", "text": "result"},
                         {"type": "image", "source": {
                             "type": "url",
                             "url": "https://example.test/i.png"}},
                     ]},
                ]},
            ],
            "max_tokens": 100,
            "system": "be brief\n\nrules",
            "stream": True,
            "temperature": 0.2,
            "top_p": 0.9,
            "tools": [{"name": "lookup", "description": "find a row",
                       "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "lookup"},
        })

    def test_max_tokens_defaults_to_32768(self):
        translated = protocol.responses_to_messages_request({
            "model": "fake-um-model",
            "input": [{"role": "user", "content": "hi"}]})
        self.assertEqual(translated["max_tokens"], 32768)
        self.assertEqual(translated["messages"], [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}])


class ChatToResponsesResponseTests(unittest.TestCase):
    """Non-stream Chat Completions payload -> Responses object."""

    def test_signature_takes_payload_model_and_keyword_only_identity(self):
        # r19 E19-1-01 (plan.md §4.1, E19-1-01 step 3): the two call sites
        # still pass positionally, and the injected identity is keyword-only so
        # no caller can pass it by accident.  The old pin was
        # ["payload", "model"]; the injection contract deliberately widened it.
        parameters = inspect.signature(
            protocol.chat_to_responses_response).parameters
        self.assertEqual(
            list(parameters),
            ["payload", "model", "created_at", "response_id"])
        for name in ("created_at", "response_id"):
            self.assertEqual(parameters[name].kind,
                             inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIsNone(parameters[name].default, name)

    def test_text_tool_calls_usage_and_incomplete_status(self):
        payload = {
            "choices": [{
                "message": {
                    "content": "hello",
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "lookup",
                                                 "arguments": "{\"q\": 1}"}}],
                },
                "finish_reason": "length",
            }],
            "usage": {
                "prompt_tokens": 10, "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": {"cached_tokens": 3},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
        before = int(time.time())
        translated = protocol.chat_to_responses_response(payload, "fake-um-model")
        after = int(time.time())
        self.assertRegex(translated["id"], r"^resp_[0-9a-f]{24}$")
        self.assertEqual(translated["object"], "response")
        self.assertIsInstance(translated["created_at"], int)
        self.assertLessEqual(before, translated["created_at"])
        self.assertLessEqual(translated["created_at"], after)
        self.assertEqual(translated["status"], "incomplete")
        self.assertEqual(translated["incomplete_details"],
                         {"reason": "max_output_tokens"})
        self.assertEqual(translated["model"], "fake-um-model")
        self.assertEqual(translated["output"][0]["type"], "message")
        self.assertEqual(translated["output"][0]["role"], "assistant")
        self.assertEqual(translated["output"][0]["content"],
                         [{"type": "output_text", "text": "hello",
                           "annotations": []}])
        self.assertEqual(translated["output"][1], {
            "type": "function_call", "id": translated["output"][1]["id"],
            "call_id": "call_1", "name": "lookup",
            "arguments": "{\"q\": 1}", "status": "completed"})
        self.assertRegex(translated["output"][1]["id"], r"^fc_[0-9a-f]{24}$")
        self.assertEqual(translated["usage"], {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2}})

    def test_malformed_or_explicit_error_payload_is_refused(self):
        for payload in (None, [], {"error": "upstream"}, {"error": {}}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    protocol.chat_to_responses_response(payload, "m")


class MessagesToResponsesResponseTests(unittest.TestCase):
    """Non-stream Anthropic Messages payload -> Responses object."""

    def test_signature_takes_payload_model_and_keyword_only_identity(self):
        # Same contract as the chat converter (plan.md §4.1, E19-1-01 step 3).
        parameters = inspect.signature(
            protocol.messages_to_responses_response).parameters
        self.assertEqual(
            list(parameters),
            ["payload", "model", "created_at", "response_id"])
        for name in ("created_at", "response_id"):
            self.assertEqual(parameters[name].kind,
                             inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIsNone(parameters[name].default, name)

    def test_text_tool_use_usage_and_stop_reason(self):
        payload = {
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "id": "toolu_1", "name": "lookup",
                 "input": {"q": 1}},
            ],
            "stop_reason": "max_tokens",
            "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            },
        }
        before = int(time.time())
        translated = protocol.messages_to_responses_response(
            payload, "fake-um-model")
        after = int(time.time())
        self.assertRegex(translated["id"], r"^resp_[0-9a-f]{24}$")
        self.assertLessEqual(before, translated["created_at"])
        self.assertLessEqual(translated["created_at"], after)
        self.assertEqual(translated["status"], "incomplete")
        self.assertEqual(translated["incomplete_details"],
                         {"reason": "max_output_tokens"})
        self.assertEqual(translated["output"][0]["type"], "message")
        self.assertEqual(translated["output"][0]["content"],
                         [{"type": "output_text", "text": "hello",
                           "annotations": []}])
        self.assertEqual(translated["output"][1], {
            "type": "function_call", "id": translated["output"][1]["id"],
            "call_id": "toolu_1", "name": "lookup",
            "arguments": "{\"q\": 1}", "status": "completed"})
        self.assertEqual(translated["usage"], {
            "input_tokens": 15, "output_tokens": 5, "total_tokens": 20,
            "input_tokens_details": {"cached_tokens": 3}})

    def test_malformed_or_explicit_error_payload_is_refused(self):
        for payload in (None, [], {"error": "upstream"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    protocol.messages_to_responses_response(payload, "m")


class ChatStreamEventsTests(unittest.TestCase):
    """Chat SSE -> Responses events."""

    def test_text_and_tool_call_are_completed_in_arrival_order(self):
        events = list(protocol.responses_stream_events(
            [CHAT_SSE], "fake-um-model", "resp_fixture", 1700000000))
        self.assertEqual(_event_names(events), [
            "response.created",
            "response.output_item.added",
            "response.output_text.delta",
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            "response.output_item.done",
            "response.output_item.done",
            "response.completed",
        ])
        self.assertEqual(
            _event_payload(events, "response.output_text.delta")["delta"],
            "hello")
        final = _event_payload(events, "response.completed")["response"]
        self.assertEqual(final["status"], "completed")
        self.assertEqual([item["type"] for item in final["output"]],
                         ["message", "function_call"])
        self.assertEqual(final["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(final["output"][1]["call_id"], "call_1")
        self.assertEqual(final["output"][1]["name"], "lookup")
        self.assertEqual(final["output"][1]["arguments"], "{\"q\":1}")
        self.assertEqual(final["usage"], {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def test_missing_done_yields_a_failed_response(self):
        stream = (b'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                  b'"finish_reason":"stop"}]}\n\n')
        events = list(protocol.responses_stream_events(
            stream, "m", "resp_fixture", 1700000000))
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["code"],
                         "upstream_eof_without_done")

    def test_length_finish_reason_is_incomplete(self):
        stream = (b'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                  b'"finish_reason":"length"}]}\n\n'
                  b'data: [DONE]\n\n')
        events = list(protocol.responses_stream_events(
            [stream], "m", "resp_fixture", 1700000000))
        self.assertEqual(events[-1][0], "response.incomplete")
        self.assertEqual(
            events[-1][1]["response"]["incomplete_details"],
            {"reason": "max_output_tokens"})

    def test_upstream_error_chunk_yields_a_failed_response(self):
        stream = b'data: {"error":{"message":"boom"}}\n\n'
        events = list(protocol.responses_stream_events(
            [stream], "m", "resp_fixture", 1700000000))
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["code"],
                         "upstream_error")


class MessagesStreamEventsTests(unittest.TestCase):
    """Anthropic Messages SSE -> Responses events."""

    def test_text_and_tool_use_are_completed(self):
        events = list(protocol.messages_stream_events(
            [ANTHROPIC_SSE], "m", "resp_fixture", 1700000000))
        self.assertEqual(_event_names(events), [
            "response.created",
            "response.output_item.added",
            "response.output_text.delta",
            "response.output_item.done",
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.delta",
            "response.output_item.done",
            "response.completed",
        ])
        final = _event_payload(events, "response.completed")["response"]
        self.assertEqual(final["status"], "completed")
        self.assertEqual([item["type"] for item in final["output"]],
                         ["message", "function_call"])
        self.assertEqual(final["output"][1]["arguments"], "{\"q\":1}")
        self.assertEqual(final["usage"], {
            "input_tokens": 7, "output_tokens": 2, "total_tokens": 9})

    def test_missing_message_stop_yields_a_failed_response(self):
        stream = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"msg_1",'
            b'"type":"message","role":"assistant","model":"m","content":[],'
            b'"usage":{"input_tokens":7,"output_tokens":0}}}\n\n'
        )
        events = list(protocol.messages_stream_events(
            [stream], "m", "resp_fixture", 1700000000))
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["code"],
                         "upstream_eof_without_message_stop")

    def test_error_event_yields_a_failed_response(self):
        stream = (b'event: error\n'
                  b'data: {"type":"error","error":{"type":"overloaded"}}\n\n')
        events = list(protocol.messages_stream_events(
            [stream], "m", "resp_fixture", 1700000000))
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["code"],
                         "upstream_error")


class SSELineTests(unittest.TestCase):

    def test_iter_lines_object_yields_stripped_non_empty_lines(self):
        class Lines:
            def iter_lines(self):
                return [b'data: {"a": 1}', b"", "event: ping"]

        self.assertEqual(list(protocol._sse_lines(Lines())),
                         ['data: {"a": 1}', "event: ping"])

    def test_raw_byte_chunks_split_lines_across_boundaries(self):
        chunks = [b'data: {"a":', b' 1}\n\n', b'data: [DONE]\n']
        self.assertEqual(list(protocol._sse_lines(chunks)),
                         ['data: {"a": 1}', 'data: [DONE]'])


class SharedModuleBoundaryTests(unittest.TestCase):

    def test_importing_shared_protocol_does_not_load_share_gateway(self):
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory() as isolated_cwd:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", CLEAN_IMPORT_PROBE,
                 str(AI_GATEWAY_ROOT)],
                cwd=isolated_cwd, env=env, capture_output=True,
                encoding="utf-8", timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        modules = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertIn("shared.responses_protocol", modules)
        self.assertNotIn("share_gateway", modules)
        self.assertNotIn("chat2api", modules)

    def test_share_gateway_reexports_the_shared_objects(self):
        spec = importlib.util.spec_from_file_location(
            "share_gateway_responses_reexport", SHARE_GATEWAY_PATH)
        share = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(share)
        for name in ("RESPONSES_FIELDS", "MESSAGES_FIELDS",
                      "valid_responses_request", "responses_to_chat_request",
                      "responses_to_messages_request",
                      "responses_to_messages_tool_choice",
                      "responses_requires_messages",
                      "chat_to_responses_response",
                      "messages_to_responses_response",
                      "responses_stream_events", "messages_stream_events",
                      "_sse_lines"):
            with self.subTest(name=name):
                self.assertIs(getattr(share, name), getattr(protocol, name))


if __name__ == "__main__":
    unittest.main()
