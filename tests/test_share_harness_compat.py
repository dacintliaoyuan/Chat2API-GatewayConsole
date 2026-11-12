"""Harness compatibility through real isolated share/upstream HTTP servers."""
import json
import unittest

import test_share_gateway as fixtures


class HarnessCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.gateway = fixtures.SharingTests()
        self.gateway.setUp()
        self.addCleanup(self.gateway.tearDown)
        self.gateway.model_payload[0] = {
            "object": "list", "data": [{"id": "model-a"}, {"id": "model-b"}]}

    def test_harness_default_chat_payload_accepts_store_false_without_forwarding_it(self):
        # Installed Harness 0.2.0-rc.2/pi-ai buildParams for an ordinary
        # custom origin: store=false, streaming usage and max_completion_tokens.
        body = {"model": "model-a", "messages": [{"role": "user", "content": "fixture"}],
                "stream": True, "stream_options": {"include_usage": True},
                "max_completion_tokens": 32, "store": False}
        status, raw = self.gateway.request(
            "POST", "/v1/chat/completions", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        self.assertIn(b"[DONE]", raw)
        forwarded = self.gateway.upstream_requests[0][2]
        expected = dict(body)
        expected.pop("store")
        self.assertEqual(forwarded, expected)

    def test_store_opt_in_and_non_boolean_values_stay_rejected(self):
        for value in (True, None, 0, 1, "false", [], {}):
            with self.subTest(value=value):
                status, raw = self.gateway.request(
                    "POST", "/v1/chat/completions", body=json.dumps({
                        "model": "model-a", "messages": [{"role": "user", "content": "fixture"}],
                        "store": value}))
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["code"], "unsupported_chat_request")
        self.assertEqual(self.gateway.upstream_requests, [])

    def test_store_false_does_not_allow_other_fields_or_messages_store(self):
        for field in ("tool_ids", "metadata", "prompt_cache_retention"):
            status, _ = self.gateway.request("POST", "/v1/chat/completions", body=json.dumps({
                "model": "model-a", "messages": [{"role": "user", "content": "fixture"}],
                "store": False, field: "unsupported"}))
            self.assertEqual(status, 400)
        status, _ = self.gateway.request("POST", "/v1/messages", body=json.dumps({
            "model": "model-a", "max_tokens": 16,
            "messages": [{"role": "user", "content": "fixture"}], "store": False}))
        self.assertEqual(status, 400)
        self.assertEqual(self.gateway.upstream_requests, [])

    def test_harness_anthropic_listing_accepts_sdk_headers_and_bounded_limit(self):
        status, raw = self.gateway.request(
            path="/v1/models?limit=1000", key=None,
            headers={"x-api-key": "test-secret", "anthropic-version": "2023-06-01"})
        self.assertEqual(status, 200, raw)
        self.assertEqual([m["id"] for m in json.loads(raw)["data"]], ["model-a", "model-b"])
        path, headers = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/models")
        self.assertEqual(headers["X-Chat2Api-Models"], "raw-fresh")
        self.assertNotIn("x-api-key", {name.lower(): value for name, value in headers.items()})
        self.assertNotIn("Authorization", headers)

    def test_limit_is_applied_to_a_fresh_catalog_and_bearer_remains_supported(self):
        for limit in (1, 1000):
            status, raw = self.gateway.request(path=f"/v1/models?limit={limit}")
            self.assertEqual(status, 200, raw)
            self.assertEqual(len(json.loads(raw)["data"]), min(limit, 2))
        self.gateway.model_payload[0] = {"object": "list", "data": [{"id": "new-model"}]}
        status, raw = self.gateway.request(path="/v1/models?limit=1000")
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["data"], [{"id": "new-model"}])

    def test_api_key_catalog_uses_the_same_authentication_and_header_conflict_checks(self):
        for path in ("/v1/models", "/v1/models?limit=1000"):
            status, raw = self.gateway.request(path=path, key=None,
                                              headers={"X-Api-Key": "test-secret"})
            self.assertEqual(status, 200, raw)
            for pairs in ([('X-Api-Key', 'wrong')],
                          [('X-Api-Key', 'test-secret'), ('X-Api-Key', 'test-secret')],
                          [('X-Api-Key', '')],
                          [('Authorization', 'Bearer test-secret'), ('X-Api-Key', 'wrong')]):
                with self.subTest(path=path, headers=[p[0] for p in pairs]):
                    status, _ = self.gateway.request_raw_headers("GET", path, pairs)
                    self.assertEqual(status, 401)
        self.assertEqual(len(self.gateway.upstream_requests), 2)

    def test_unknown_queries_and_non_catalog_query_paths_stay_closed(self):
        paths = ("/v1/models?limit=0", "/v1/models?limit=1001", "/v1/models?limit=-1",
                 "/v1/models?limit=01", "/v1/models?limit=1.0", "/v1/models?limit=",
                 "/v1/models?limit=1000&limit=1", "/v1/models?limit=1000&x=1",
                 "/v1/models?%6cimit=1000", "/v1/models?limit=%31", "/v1/models?",
                 "/v1/models?limit=1#fragment", "/v1/models/?limit=1",
                 "/v1/%6dodels?limit=1", "/v1/v1/models?limit=1")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.gateway.request(path=path)[0], 404)
        self.assertEqual(self.gateway.request("POST", "/v1/chat/completions?limit=1")[0], 404)
        self.assertEqual(self.gateway.request("POST", "/v1/messages?limit=1")[0], 404)
        self.assertEqual(self.gateway.upstream_requests, [])

    def test_anthropic_beta_query_is_consumed_locally(self):
        body = {"model": "model-a", "max_tokens": 16,
                "messages": [{"role": "user", "content": "fixture"}]}
        for path in ("/v1/messages", "/v1/messages?beta=true"):
            with self.subTest(path=path):
                status, raw = self.gateway.request(
                    "POST", path, body=json.dumps(body))
                self.assertEqual(status, 200, raw)
                self.assertIn(b"hello", raw)
        for path in ("/v1/models?beta=true",
                     "/v1/models?limit=1000&beta=true"):
            with self.subTest(path=path):
                status, raw = self.gateway.request(path=path)
                self.assertEqual(status, 200, raw)
        paths = [request[0] for request in self.gateway.upstream_requests]
        self.assertEqual(paths, ["/v1/messages", "/v1/messages",
                               "/v1/models", "/v1/models"])

    def test_anthropic_beta_query_rejects_unknown_or_false_values(self):
        body = json.dumps({"model": "model-a", "max_tokens": 16,
                            "messages": [{"role": "user", "content": "x"}]})
        for path in ("/v1/messages?beta=false",
                     "/v1/messages?beta=true&x=1",
                     "/v1/messages?beta=1",
                     "/v1/models?beta=false",
                     "/v1/models?beta=true&x=1"):
            with self.subTest(path=path):
                status, raw = self.gateway.request(
                    "POST", path, body=body) if path.startswith(
                        "/v1/messages") else self.gateway.request(path=path)
                self.assertEqual(status, 404, raw)
        self.assertEqual(self.gateway.upstream_requests, [])

    def test_queried_catalog_still_requires_confirmed_upstream_freshness(self):
        self.gateway.model_marker[0] = False
        status, raw = self.gateway.request(path="/v1/models?limit=1000")
        self.assertEqual(status, 502, raw)
        self.assertEqual(json.loads(raw)["error"]["code"], "upstream_catalog_freshness_unconfirmed")

    def test_responses_relays_backend_stream_verbatim(self):
        self.gateway.responses_payload[0] = RESPONSES_SSE
        body = {"model": "model-a", "input": "fixture", "stream": True,
                "store": False, "max_output_tokens": 32}
        status, raw = self.gateway.request(
            "POST", "/v1/responses", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_SSE)
        path, headers, forwarded = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(forwarded, body)

    def test_responses_relays_backend_json_verbatim(self):
        self.gateway.responses_payload[0] = RESPONSES_JSON
        body = {"model": "model-a", "input": "fixture"}
        status, raw = self.gateway.request(
            "POST", "/v1/responses", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_JSON)
        path, headers, forwarded = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(forwarded, body)

    def test_responses_forwards_message_items_without_an_explicit_type(self):
        self.gateway.responses_payload[0] = RESPONSES_JSON
        body = {"model": "model-a", "stream": False,
                "input": [{"role": "user",
                           "content": [{"type": "input_text",
                                        "text": "fixture"}]}]}
        status, raw = self.gateway.request(
            "POST", "/v1/responses", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        path, headers, forwarded = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(forwarded["input"], body["input"])

    def test_responses_rejects_store_true_unknown_fields_and_bad_input(self):
        bodies = (
            {"model": "model-a", "input": "fixture", "store": True},
            {"model": "model-a", "input": "fixture", "metadata": {}},
            {"model": "model-a", "input": ""},
            {"model": "model-a", "input": "fixture", "stream": "yes"},
            {"model": "model-a", "input": "fixture", "max_output_tokens": 0},
            {"model": "model-a", "input": "fixture", "temperature": True},
        )
        for body in bodies:
            with self.subTest(body=body):
                status, raw = self.gateway.request(
                    "POST", "/v1/responses", body=json.dumps(body))
                self.assertEqual(status, 400, raw)
                self.assertEqual(json.loads(raw)["error"]["code"],
                                 "unsupported_responses_request")
        self.assertEqual(self.gateway.upstream_requests, [])

    def test_responses_relays_backend_function_tool_stream(self):
        self.gateway.responses_payload[0] = RESPONSES_TOOL_SSE
        body = {
            "model": "model-a",
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "fixture"}]}],
            "stream": True,
            "tools": [{"type": "function", "name": "get_weather",
                       "description": "Weather", "parameters": {"type": "object"}}]}
        status, raw = self.gateway.request(
            "POST", "/v1/responses", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_TOOL_SSE)
        path, headers, forwarded = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(forwarded["tools"], body["tools"])

    def test_responses_forwards_reasoning_fields_unchanged(self):
        self.gateway.responses_payload[0] = RESPONSES_JSON
        body = {"model": "model-a", "input": "fixture",
                "reasoning": {"effort": "low", "summary": "auto"},
                "include": ["reasoning.encrypted_content"]}
        status, raw = self.gateway.request(
            "POST", "/v1/responses", body=json.dumps(body))
        self.assertEqual(status, 200, raw)
        path, headers, forwarded = self.gateway.upstream_requests[0]
        self.assertEqual(path, "/v1/responses")
        self.assertEqual(forwarded["reasoning"], body["reasoning"])
        self.assertEqual(forwarded["include"], body["include"])
        self.assertNotIn("reasoning_effort", forwarded)

    def test_responses_relays_backend_incomplete_status(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason):
                expected = ("max_output_tokens" if reason == "length"
                            else "content_filter")
                self.gateway.responses_payload[0] = json.dumps({
                    "id": "resp_1", "object": "response", "created_at": 1,
                    "status": "incomplete", "model": "model-a",
                    "output": [],
                    "incomplete_details": {"reason": expected},
                    "usage": {"input_tokens": 1, "output_tokens": 2,
                              "total_tokens": 3}}).encode()
                status, raw = self.gateway.request(
                    "POST", "/v1/responses",
                    body=json.dumps({"model": "model-a", "input": "fixture"}))
                self.assertEqual(status, 200, raw)
                payload = json.loads(raw)
                self.assertEqual(payload["status"], "incomplete")
                self.assertEqual(payload["incomplete_details"]["reason"],
                                 expected)

    def test_responses_relays_backend_failed_stream(self):
        self.gateway.responses_payload[0] = RESPONSES_FAILED_SSE
        status, raw = self.gateway.request(
            "POST", "/v1/responses",
            body=json.dumps({"model": "model-a", "input": "fixture",
                             "stream": True}))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_FAILED_SSE)
        self.assertIn(b"event: response.failed", raw)

    def test_responses_relays_backend_incomplete_stream(self):
        self.gateway.responses_payload[0] = RESPONSES_INCOMPLETE_SSE
        status, raw = self.gateway.request(
            "POST", "/v1/responses",
            body=json.dumps({"model": "model-a", "input": "fixture",
                             "stream": True}))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_INCOMPLETE_SSE)
        self.assertIn(b"event: response.incomplete", raw)

    def test_responses_relays_backend_zero_argument_tool_call(self):
        self.gateway.responses_payload[0] = RESPONSES_ZERO_TOOL_SSE
        status, raw = self.gateway.request(
            "POST", "/v1/responses",
            body=json.dumps({"model": "model-a", "input": "fixture",
                             "stream": True}))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_ZERO_TOOL_SSE)
        self.assertEqual(raw.count(b"event: response.output_item.done"), 1)

    def test_responses_translation_merges_consecutive_function_calls(self):
        messages = fixtures.share.responses_to_chat_messages({
            "model": "model-a", "input": [
                {"type": "function_call", "call_id": "c1", "name": "a",
                 "arguments": "{}"},
                {"type": "function_call", "call_id": "c2", "name": "b",
                 "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1",
                 "output": "1"},
                {"type": "function_call_output", "call_id": "c2",
                 "output": "2"}]})
        self.assertEqual(len(messages), 3)
        self.assertEqual([call["id"] for call in messages[0]["tool_calls"]],
                         ["c1", "c2"])
        self.assertEqual([message["role"] for message in messages],
                         ["assistant", "tool", "tool"])

    def test_responses_translation_maps_function_tool_choice(self):
        forwarded = fixtures.share.responses_to_chat_request({
            "model": "model-a", "input": "fixture",
            "tools": [{"type": "function", "name": "get_weather",
                       "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "get_weather"}})
        self.assertEqual(forwarded["tool_choice"], {
            "type": "function", "function": {"name": "get_weather"}})

    def test_responses_relays_backend_usage_details(self):
        self.gateway.responses_payload[0] = RESPONSES_DETAILS_JSON
        status, raw = self.gateway.request(
            "POST", "/v1/responses",
            body=json.dumps({"model": "model-a", "input": "fixture"}))
        self.assertEqual(status, 200, raw)
        self.assertEqual(raw, RESPONSES_DETAILS_JSON)
        usage = json.loads(raw)["usage"]
        self.assertEqual(usage["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(usage["output_tokens_details"]["reasoning_tokens"], 1)

    def test_responses_requires_messages_only_for_image_tool_results(self):
        text_body = {"model": "model-a", "input": [
            {"type": "function_call_output", "call_id": "c1",
             "output": "plain text"}]}
        image_body = {"model": "model-a", "input": [
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "shot"},
                        {"type": "input_image",
                         "image_url": "data:image/png;base64,AAAA"}]}]}
        self.assertFalse(fixtures.share.responses_requires_messages(text_body))
        self.assertTrue(fixtures.share.responses_requires_messages(image_body))

    def test_responses_to_messages_request_keeps_tool_result_images(self):
        body = {"model": "model-a", "instructions": "sys", "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call", "call_id": "c1", "name": "shot",
             "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1",
             "output": [{"type": "input_text", "text": "shot"},
                        {"type": "input_image",
                         "image_url": "data:image/png;base64,AAAA"}]}],
            "max_output_tokens": 64, "stream": True,
            "tools": [{"type": "function", "name": "shot",
                       "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "shot"}}
        translated = fixtures.share.responses_to_messages_request(body)
        self.assertEqual(translated["max_tokens"], 64)
        self.assertEqual(translated["system"], "sys")
        self.assertTrue(translated["stream"])
        self.assertEqual(translated["tools"], [
            {"name": "shot", "input_schema": {"type": "object"}}])
        self.assertEqual(translated["tool_choice"],
                         {"type": "tool", "name": "shot"})
        messages = translated["messages"]
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"][0]["type"], "tool_use")
        self.assertEqual(messages[1]["content"][0]["input"], {})
        tool_result = messages[2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "c1")
        self.assertEqual(tool_result["content"][1]["type"], "image")
        self.assertEqual(tool_result["content"][1]["source"]["media_type"],
                         "image/png")
        self.assertEqual(tool_result["content"][1]["source"]["data"], "AAAA")

    def test_messages_to_responses_response_maps_text_tool_use_and_limits(self):
        payload = {"type": "message", "role": "assistant", "content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "toolu_1", "name": "shot",
             "input": {"x": 1}}], "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "cache_read_input_tokens": 3,
                      "cache_creation_input_tokens": 2,
                      "output_tokens": 5}}
        translated = fixtures.share.messages_to_responses_response(
            payload, "model-a")
        self.assertEqual(translated["status"], "incomplete")
        self.assertEqual(translated["incomplete_details"]["reason"],
                         "max_output_tokens")
        self.assertEqual(translated["usage"]["input_tokens"], 15)
        self.assertEqual(
            translated["usage"]["input_tokens_details"]["cached_tokens"], 3)
        self.assertEqual(translated["output"][0]["content"][0]["text"], "hi")
        self.assertEqual(translated["output"][1]["type"], "function_call")
        self.assertEqual(translated["output"][1]["arguments"], '{"x": 1}')

    def test_messages_stream_events_maps_text_tool_use_and_stop_reason(self):
        events = list(fixtures.share.messages_stream_events(
            iter(ANTHROPIC_STREAM_FRAMES), "model-a", "resp_1", 1))
        names = [name for name, payload in events]
        self.assertIn("response.created", names)
        self.assertIn("response.output_item.added", names)
        self.assertIn("response.output_text.delta", names)
        self.assertIn("response.function_call_arguments.delta", names)
        self.assertIn("response.output_item.done", names)
        self.assertIn("response.completed", names)
        completed = [payload for name, payload in events
                     if name == "response.completed"][0]
        self.assertEqual(completed["response"]["output"][1]["type"],
                         "function_call")
        self.assertEqual(completed["response"]["output"][1]["arguments"],
                         '{"x":1}')

    def test_messages_stream_without_message_stop_is_failed(self):
        frames = [b'event: message_start\ndata: {"type":"message_start"}\n\n']
        events = list(fixtures.share.messages_stream_events(
            iter(frames), "model-a", "resp_1", 1))
        self.assertEqual(events[-1][0], "response.failed")
        self.assertEqual(events[-1][1]["response"]["error"]["code"],
                         "upstream_eof_without_message_stop")


RESPONSES_SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":'
    b'{"id":"resp_1","object":"response","created_at":1,"status":'
    b'"in_progress","model":"model-a","output":[]}}\n\n'
    b'event: response.output_item.added\ndata: {"type":'
    b'"response.output_item.added","output_index":0,"item":{"type":"message",'
    b'"id":"msg_1","status":"in_progress","role":"assistant","content":[]}}\n\n'
    b'event: response.output_text.delta\ndata: {"type":'
    b'"response.output_text.delta","item_id":"msg_1","output_index":0,'
    b'"content_index":0,"delta":"fixture reply"}\n\n'
    b'event: response.output_item.done\ndata: {"type":'
    b'"response.output_item.done","output_index":0,"item":{"type":"message",'
    b'"id":"msg_1","status":"completed","role":"assistant","content":'
    b'[{"type":"output_text","text":"fixture reply","annotations":[]}]}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed",'
    b'"response":{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"completed","model":"model-a","output":[{"type":"message",'
    b'"id":"msg_1","status":"completed","role":"assistant","content":'
    b'[{"type":"output_text","text":"fixture reply","annotations":[]}]}],'
    b'"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
)
RESPONSES_JSON = (
    b'{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"completed","model":"model-a","output":[{"type":"message",'
    b'"id":"msg_1","status":"completed","role":"assistant","content":'
    b'[{"type":"output_text","text":"fixture reply","annotations":[]}]}],'
    b'"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}'
)
RESPONSES_TOOL_SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":'
    b'{"id":"resp_1","object":"response","created_at":1,"status":'
    b'"in_progress","model":"model-a","output":[]}}\n\n'
    b'event: response.output_item.added\ndata: {"type":'
    b'"response.output_item.added","output_index":0,"item":{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_weather",'
    b'"arguments":"","status":"in_progress"}}\n\n'
    b'event: response.function_call_arguments.delta\ndata: {"type":'
    b'"response.function_call_arguments.delta","item_id":"fc_1",'
    b'"output_index":0,"delta":"{\\"city\\":\\"Macau\\"}"}\n\n'
    b'event: response.output_item.done\ndata: {"type":'
    b'"response.output_item.done","output_index":0,"item":{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_weather",'
    b'"arguments":"{\\"city\\":\\"Macau\\"}","status":"completed"}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed",'
    b'"response":{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"completed","model":"model-a","output":[{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_weather",'
    b'"arguments":"{\\"city\\":\\"Macau\\"}","status":"completed"}],'
    b'"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
)
RESPONSES_FAILED_SSE = (
    b'event: response.failed\ndata: {"type":"response.failed","response":'
    b'{"id":"resp_1","object":"response","created_at":1,"status":"failed",'
    b'"model":"model-a","output":[],"error":{"code":'
    b'"upstream_eof_without_done","message":"upstream stream ended '
    b'without [DONE]"}}}\n\n'
)
RESPONSES_INCOMPLETE_SSE = (
    b'event: response.incomplete\ndata: {"type":"response.incomplete",'
    b'"response":{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"incomplete","model":"model-a","output":[],'
    b'"incomplete_details":{"reason":"max_output_tokens"},'
    b'"usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}}\n\n'
)
RESPONSES_ZERO_TOOL_SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":'
    b'{"id":"resp_1","object":"response","created_at":1,"status":'
    b'"in_progress","model":"model-a","output":[]}}\n\n'
    b'event: response.output_item.added\ndata: {"type":'
    b'"response.output_item.added","output_index":0,"item":{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_time",'
    b'"arguments":"","status":"in_progress"}}\n\n'
    b'event: response.output_item.done\ndata: {"type":'
    b'"response.output_item.done","output_index":0,"item":{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_time",'
    b'"arguments":"","status":"completed"}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed",'
    b'"response":{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"completed","model":"model-a","output":[{"type":'
    b'"function_call","id":"fc_1","call_id":"call_1","name":"get_time",'
    b'"arguments":"","status":"completed"}],"usage":{"input_tokens":3,'
    b'"output_tokens":2,"total_tokens":5}}}\n\n'
)
RESPONSES_DETAILS_JSON = (
    b'{"id":"resp_1","object":"response","created_at":1,'
    b'"status":"completed","model":"model-a","output":[{"type":"message",'
    b'"id":"msg_1","status":"completed","role":"assistant","content":'
    b'[{"type":"output_text","text":"fixture reply","annotations":[]}]}],'
    b'"usage":{"input_tokens":4,"output_tokens":3,"total_tokens":7,'
    b'"input_tokens_details":{"cached_tokens":2},'
    b'"output_tokens_details":{"reasoning_tokens":1}}}'
)
ANTHROPIC_STREAM_FRAMES = [
    b'event: message_start\ndata: {"type":"message_start","message":'
    b'{"id":"msg_1","type":"message","role":"assistant","content":[],'
    b'"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":10,'
    b'"output_tokens":0}}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start",'
    b'"index":0,"content_block":{"type":"text","text":""}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta",'
    b'"index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop",'
    b'"index":0}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start",'
    b'"index":1,"content_block":{"type":"tool_use","id":"toolu_1",'
    b'"name":"shot","input":{}}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta",'
    b'"index":1,"delta":{"type":"input_json_delta",'
    b'"partial_json":"{\\"x\\":1}"}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop",'
    b'"index":1}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":'
    b'{"stop_reason":"tool_use","stop_sequence":null},'
    b'"usage":{"output_tokens":5}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


if __name__ == "__main__":
    unittest.main()
