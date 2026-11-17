# -*- coding: utf-8 -*-
"""E19-1-02: the non-stream aggregator is owned by ``shared``.

Both gateway applications serve ``stream=false`` clients from an aggregated SSE
upstream, so the decoder lives in ``shared/nonstream_aggregate.py``.

These tests pin two things:

* the shared module is the implementation;
* the SSE samples aggregate to the expected body, and that body is checked
  against the wire contract explicitly instead of only against itself.
"""

import ast
import sys
import unittest
from pathlib import Path

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULE_PATH = AI_GATEWAY_ROOT / "shared" / "nonstream_aggregate.py"

sys.path.insert(0, str(AI_GATEWAY_ROOT))

from shared import nonstream_aggregate as shared_aggregate  # noqa: E402


CHAT_SSE = (
    b'data: {"id":"chatcmpl-1","created":1700000000,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
    b'data: {"id":"chatcmpl-1","created":1700000000,"model":"m",'
    b'"choices":[{"index":0,"delta":{"content":"lo","reasoning":"think-"}}]}\n\n'
    b'data: {"id":"chatcmpl-1","created":1700000000,"model":"m",'
    b'"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    b'"id":"call_1","type":"function","function":{"name":"lookup",'
    b'"arguments":"{\\"q\\":"}}]}}]}\n\n'
    b'data: {"id":"chatcmpl-1","created":1700000000,"model":"m",'
    b'"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
    b'"function":{"arguments":"1}"}}]},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},'
    b'"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":10,'
    b'"completion_tokens":5,"total_tokens":15},'
    b'"system_fingerprint":"fp_1"}\n\n'
    b'data: [DONE]\n\n'
)

CHAT_EXPECTED = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1700000000,
    "model": "m",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "Hello",
            "reasoning": "think-",
            "tool_calls": [{
                "function": {"arguments": '{"q":1}', "name": "lookup"},
                "id": "call_1",
                "type": "function",
            }],
        },
        "finish_reason": "tool_calls",
    }],
    "system_fingerprint": "fp_1",
    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
              "total_tokens": 15},
}

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
    b'event: message_delta\n'
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    b'"stop_sequence":null},"usage":{"output_tokens":2}}\n\n'
    b'event: message_stop\n'
    b'data: {"type":"message_stop"}\n\n'
)

ANTHROPIC_EXPECTED = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 7, "output_tokens": 2},
}


def _aggregate(module, aggregator_name, payload):
    aggregator = getattr(module, aggregator_name)()
    aggregator.feed_bytes(payload)
    return aggregator.finish()


class SharedNonstreamAggregateTests(unittest.TestCase):

    def test_shared_module_is_the_implementation(self):
        self.assertTrue(SHARED_MODULE_PATH.is_file(),
                        "shared/nonstream_aggregate.py must exist")
        self.assertEqual(
            Path(shared_aggregate.__file__).resolve(),
            SHARED_MODULE_PATH.resolve(),
            "the imported aggregator must come from shared/",
        )
        self.assertIn("shared", (shared_aggregate.__doc__ or "").lower(),
                      "the shared module docstring must name its home")

    def test_chat_sample_aggregates_to_the_wire_contract(self):
        body = _aggregate(
            shared_aggregate, "ChatCompletionAggregator", CHAT_SSE)
        self.assertEqual(body, CHAT_EXPECTED)

    def test_anthropic_sample_aggregates_to_the_wire_contract(self):
        body = _aggregate(
            shared_aggregate, "AnthropicMessageAggregator", ANTHROPIC_SSE)
        self.assertEqual(body, ANTHROPIC_EXPECTED)

    def test_a_field_the_upstream_never_sent_stays_absent(self):
        """spec-shared-08: the module docstring promises no fabrication.

        ``created`` used to be filled with ``int(time.time())`` when the upstream
        never sent one, so the shared aggregator wrote the HOST CLOCK into the
        protocol output — exactly what "a field the upstream never sent stays
        absent; nothing is fabricated" forbids.
        """
        payload = (
            b'data: {"id":"chatcmpl-1","model":"m",'
            b'"choices":[{"index":0,"delta":{"role":"assistant",'
            b'"content":"hi"}}]}\n\n'
            b'data: {"choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )
        body = _aggregate(
            shared_aggregate, "ChatCompletionAggregator", payload)
        self.assertNotIn("created", body,
                         "a created value the upstream never sent must stay "
                         "absent: %r" % (body,))
        self.assertEqual(body["id"], "chatcmpl-1")
        self.assertEqual(body["choices"][0]["message"]["content"], "hi")

    def test_shared_module_imports_only_stdlib(self):
        tree = ast.parse(SHARED_MODULE_PATH.read_text(encoding="utf-8-sig"),
                         filename=str(SHARED_MODULE_PATH))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots.add((node.module or "").split(".")[0])
        allowed = {"json", "time", "dataclasses"}
        self.assertEqual(roots - allowed, set(),
                         "the shared aggregator may only import stdlib pure "
                         "computation modules")


if __name__ == "__main__":
    unittest.main()
