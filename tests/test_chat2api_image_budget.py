# -*- coding: utf-8 -*-
"""Chat image-budget contract (16-image limit, protected current turn).

Production evidence: UM rejects prompts with >16 images (HTTP 400,
``{"detail": "At most 16 image(s) may be provided in one prompt..."}``),
which hit the giant 接手 conversations as a 400 storm. chat2api now trims
HISTORICAL images (oldest first, newest kept, exact 16) once per downstream
request, never silently drops the current turn by default (local 400 instead),
and — with the explicit ``CHAT2API_AUTO_TRIM_CURRENT_TURN_IMAGES=1`` opt-in —
drops the oldest current-turn images down to the limit so unattended agents
self-heal.

These tests pin the trimming contract at the pure-function level (no
upstream needed) plus one HTTP-level case proving the local 400 sends zero
upstream requests.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chat2api_http_harness as harness  # noqa: E402

CHAT2API_PATH = Path(__file__).resolve().parents[1] / "chat2api" / "chat2api.py"
SPEC = importlib.util.spec_from_file_location(
    "chat2api_image_budget", CHAT2API_PATH)
chat2api = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = chat2api
SPEC.loader.exec_module(chat2api)

LIMIT = chat2api.MAX_IMAGES
STUB_TOKEN = "stub"


def _img_user_message(count, text=None):
    parts = [{"type": "image_url", "image_url": {"url": f"img-{i}"}} for i in range(count)]
    if text:
        parts.append({"type": "text", "text": text})
    return {"role": "user", "content": parts}


def _img_parts(n):
    return [{"type": "image_url", "image_url": {"url": f"h-{i}"}} for i in range(n)]


def _labeled_img_parts(prefix, n):
    return [{"type": "image_url", "image_url": {"url": f"{prefix}-{i}"}}
            for i in range(n)]


def _surviving_urls(body):
    return [p["image_url"]["url"] for m in body["messages"]
            for p in (m.get("content") or [] if isinstance(m.get("content"), list) else [])
            if isinstance(p, dict) and p.get("type") == "image_url"]


def _count_images(body):
    stats = chat2api._chat_image_stats(body)
    return stats["images_total"]


class TestChat2ApiImageBudget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    # ---------- pure-function trimming contract ----------

    def test_trims_oldest_history_keeps_newest_exact_limit(self):
        # 20 historical images + 2 current-turn images: keep the newest 14
        # historical + the 2 protected ones, exactly LIMIT total. Labels are
        # globally unique so the assertions pin WHICH images survive, not
        # just how many.
        messages = [{"role": "user",
                     "content": _labeled_img_parts("hist-a", 1)}]
        messages.append({"role": "assistant", "content": "ok"})
        messages.append({"role": "user", "content":
                         _labeled_img_parts("hist-b", 19)})
        messages.append({"role": "assistant", "content": "mid"})
        messages.append({"role": "user", "content":
                         _labeled_img_parts("cur", 2) +
                         [{"type": "text", "text": "current question"}]})
        body = {"model": "GLM-5.3-Flash", "stream": True,
                "messages": messages}
        self.assertEqual(_count_images(body), 22)

        out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertEqual(stats["images_after"], LIMIT)
        self.assertEqual(stats["images_trimmed"],
                         stats["images_before"] - LIMIT)
        self.assertEqual(_count_images(out), LIMIT)
        surviving = [p["image_url"]["url"] for m in out["messages"]
                     for p in (m.get("content") or [] if isinstance(m.get("content"), list) else [])
                     if isinstance(p, dict) and p.get("type") == "image_url"]
        # Oldest-first across history: hist-a-0 (the 1-image message, fully
        # emptied) plus the five oldest of the 19-image message are gone.
        self.assertEqual(surviving,
                         [f"hist-b-{i}" for i in range(5, 19)] +
                         ["cur-0", "cur-1"])
        self.assertEqual(len(surviving), LIMIT)
        # The fully-emptied oldest message keeps ONE text placeholder.
        emptied = out["messages"][0]["content"]
        self.assertEqual(len(emptied), 1)
        self.assertEqual(emptied[0]["type"], "text")
        # Copy-on-write: the original body is untouched.
        self.assertEqual(_count_images(body), 22)

    def test_image_only_message_gets_placeholder(self):
        # A 1-image old message followed by a 16-image one + a text-only
        # current turn: exactly one image must go, and oldest-first means the
        # 1-image message is fully emptied -> ONE text placeholder.
        messages = [{"role": "user", "content": _img_parts(1)}]
        messages.append({"role": "user", "content": _img_parts(16)})
        messages.append({"role": "assistant", "content": "a"})
        messages.append({"role": "user", "content": "text-only"})
        body = {"model": "m", "stream": True, "messages": messages}
        out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertEqual(stats["images_after"], LIMIT)
        emptied = out["messages"][0]
        self.assertEqual(emptied["role"], "user")
        self.assertEqual(len(emptied["content"]), 1)
        self.assertEqual(emptied["content"][0]["type"], "text")

    def test_current_turn_over_limit_rejected_locally(self):
        messages = [_img_user_message(LIMIT + 1, text="too many now")]
        body = {"model": "m", "stream": True, "messages": messages}
        out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNotNone(err)
        self.assertEqual(err["type"], "too_many_images_current_turn")
        # Copy-on-write: unchanged payload returned for the 400 path.
        self.assertIs(out, body)
        self.assertEqual(stats["images_before"], LIMIT + 1)

    def test_auto_trim_disabled_keeps_original(self):
        messages = [_img_user_message(LIMIT + 5)]
        body = {"model": "m", "stream": True, "messages": messages}
        with mock.patch.object(chat2api, "AUTO_TRIM_HISTORICAL_IMAGES", False):
            out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertIs(out, body)
        self.assertEqual(stats["images_after"], stats["images_before"])

    def test_under_limit_is_identity(self):
        messages = [_img_user_message(3), {"role": "assistant", "content": "x"},
                    {"role": "user", "content": "next"}]
        body = {"model": "m", "stream": True, "messages": messages}
        out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertIs(out, body)
        self.assertEqual(stats["images_trimmed"], 0)

    def test_non_image_parts_never_touched(self):
        # A dict that merely happens to carry an "image"-ish field must
        # never be classified or removed.
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "tool_result", "url": "x", "image": "NOT-AN-IMAGE-PART"}]},
            {"role": "user", "content": "current"},
        ]
        body = {"model": "m", "stream": True, "messages": messages}
        out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertEqual(stats["images_total"], 0)

    def test_anthropic_input_image_shape_recognized(self):
        messages = [
            {"role": "user", "content": [
                {"type": "input_image", "image_url": "img-a"},
                {"type": "input_image", "image_url": "img-b"}]},
            {"role": "user", "content": "next"},
        ]
        body = {"model": "m", "stream": True, "messages": messages}
        self.assertEqual(_count_images(body), 2)

    # ---------- error body extraction ----------

    def test_detail_error_format_extracted(self):
        # UM's actual 400 body wraps the message under "detail", not "error".
        message, err_type, err_code = chat2api._extract_upstream_reject_error(
            json.dumps({"detail": "At most 16 image(s) may be provided in "
                                "one prompt."}))
        self.assertIn("At most 16 image(s)", message)


class TestChat2ApiCurrentTurnTrim(unittest.TestCase):
    """Opt-in current-turn trim (CHAT2API_AUTO_TRIM_CURRENT_TURN_IMAGES=1).

    Default stays the local 400 (rejected, zero mutation); with the flag on,
    the newest MAX_IMAGES current-turn images survive, the oldest are dropped
    locally, and history is fully trimmed because the current turn occupies
    the whole budget.
    """

    def test_keeps_newest_current_turn_exact_limit(self):
        # 10 historical + 21 current-turn images (agent/tool parts AFTER the
        # last user message) -> exactly LIMIT total, with the OLDEST 5
        # current-turn images dropped and all history gone.
        messages = [{"role": "user", "content":
                     _labeled_img_parts("old-a", 4) +
                     [{"type": "text", "text": "older turn"}]},
                    {"role": "assistant", "content": "mid"},
                    {"role": "user", "content":
                     _labeled_img_parts("old-b", 6)},
                    {"role": "user", "content":
                     _labeled_img_parts("cur-a", 9) +
                     [{"type": "text", "text": "look at these"}]},
                    {"role": "assistant", "content":
                     [{"type": "text", "text": "scanning"}] +
                     _labeled_img_parts("cur-b", 7)},
                    {"role": "tool", "content":
                     _labeled_img_parts("cur-c", 5) +
                     [{"type": "text", "text": "final question"}]}]
        body = {"model": "m", "stream": True, "messages": messages}
        self.assertEqual(chat2api._chat_image_stats(body)["images_total"], 31)
        self.assertEqual(
            chat2api._chat_image_stats(body)["current_turn_images"], 21)

        with mock.patch.object(chat2api,
                               "AUTO_TRIM_CURRENT_TURN_IMAGES", True):
            out, stats, err = chat2api._normalize_chat_image_budget(body)

        self.assertIsNone(err)
        self.assertEqual(stats["images_before"], 31)
        self.assertEqual(stats["images_after"], LIMIT)
        self.assertEqual(stats["images_trimmed"], 31 - LIMIT)
        self.assertEqual(stats["historical_removed"], 10)
        self.assertEqual(stats["current_turn_trimmed"], 5)
        # Newest current-turn images survive (cur-a-4..8, all cur-b, all
        # cur-c); the oldest five (cur-a-0..4) are gone locally.
        surviving = _surviving_urls(out)
        self.assertEqual(len(surviving), LIMIT)
        for gone in ("cur-a-0", "cur-a-1", "cur-a-2", "cur-a-3", "cur-a-4"):
            self.assertNotIn(gone, surviving)
        for kept in ("cur-a-8", "cur-b-0", "cur-b-6", "cur-c-0", "cur-c-4"):
            self.assertIn(kept, surviving)
        self.assertNotIn("old-a-0", surviving)
        # Copy-on-write: the original body is untouched.
        self.assertEqual(chat2api._chat_image_stats(body)["images_total"], 31)
        # The text part of the first current message survived the trim.
        first_current = out["messages"][3]["content"]
        self.assertTrue(any(p.get("type") == "text" for p in first_current))

    def test_emptied_current_turn_message_gets_current_turn_placeholder(self):
        # A protected image-only message whose single image is trimmed must
        # receive the CURRENT-TURN placeholder, not the historical one.
        messages = [{"role": "user", "content": _img_parts(1)},
                    {"role": "user", "content": _img_parts(2)},
                    {"role": "assistant", "content": "a"},
                    {"role": "user", "content": "go"},
                    {"role": "tool", "content": _labeled_img_parts("shot", 1)},
                    {"role": "tool", "content": _labeled_img_parts("now", 16)}]
        body = {"model": "m", "stream": True, "messages": messages}
        self.assertEqual(chat2api._chat_image_stats(body)["images_total"], 20)

        with mock.patch.object(chat2api,
                               "AUTO_TRIM_CURRENT_TURN_IMAGES", True):
            out, stats, err = chat2api._normalize_chat_image_budget(body)

        self.assertIsNone(err)
        self.assertEqual(stats["images_after"], LIMIT)
        self.assertEqual(stats["current_turn_trimmed"], 1)
        self.assertEqual(stats["historical_removed"], 3)
        shot_msg = out["messages"][4]
        self.assertEqual(len(shot_msg["content"]), 1)
        self.assertEqual(shot_msg["content"][0]["text"],
                         chat2api._CURRENT_TURN_IMAGE_PLACEHOLDER)
        # Both emptied history messages keep the historical placeholder.
        for idx in (0, 1):
            emptied = out["messages"][idx]["content"]
            self.assertEqual(len(emptied), 1)
            self.assertEqual(emptied[0]["text"],
                             chat2api._HISTORICAL_IMAGE_PLACEHOLDER)

    def test_without_user_boundary_everything_is_current_turn(self):
        # No user message: every image is conservatively protected, so an
        # enabled trim drops the oldest four down to the newest LIMIT.
        messages = [{"role": "assistant", "content":
                     [{"type": "text", "text": "run"}] +
                     _labeled_img_parts("s", 20)}]
        body = {"model": "m", "stream": True, "messages": messages}

        with mock.patch.object(chat2api,
                               "AUTO_TRIM_CURRENT_TURN_IMAGES", True):
            out, stats, err = chat2api._normalize_chat_image_budget(body)

        self.assertIsNone(err)
        self.assertEqual(stats["images_after"], LIMIT)
        self.assertEqual(stats["current_turn_trimmed"], 4)
        self.assertEqual(stats["historical_removed"], 0)
        surviving = _surviving_urls(out)
        self.assertEqual(surviving,
                         [f"s-{i}" for i in range(4, 20)])

    def test_master_gate_historical_disabled_keeps_everything(self):
        # AUTO_TRIM_HISTORICAL_IMAGES=0 remains the master switch for any
        # payload rewrite: the current-turn opt-in alone must do nothing.
        messages = [{"role": "user", "content": _img_parts(LIMIT + 3)}]
        body = {"model": "m", "stream": True, "messages": messages}
        with mock.patch.object(chat2api, "AUTO_TRIM_HISTORICAL_IMAGES",
                               False), \
             mock.patch.object(chat2api, "AUTO_TRIM_CURRENT_TURN_IMAGES",
                               True):
            out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertIsNone(err)
        self.assertIs(out, body)
        self.assertEqual(stats["images_after"], LIMIT + 3)
        self.assertEqual(stats["current_turn_trimmed"], 0)

    def test_default_off_rejects_current_turn_locally(self):
        # Default (flag absent): >LIMIT current-turn images still return the
        # local 400 contract and an unchanged payload.
        messages = [_img_user_message(LIMIT + 1, text="still default off")]
        body = {"model": "m", "stream": True, "messages": messages}
        with mock.patch.object(chat2api,
                               "AUTO_TRIM_CURRENT_TURN_IMAGES", False):
            out, stats, err = chat2api._normalize_chat_image_budget(body)
        self.assertEqual(err["type"], "too_many_images_current_turn")
        self.assertIs(out, body)
        self.assertEqual(stats["current_turn_trimmed"], 0)


class _CountingUpstream(BaseHTTPRequestHandler):
    """Minimal upstream stub: counts real POSTs, answers a tiny SSE stream."""

    protocol_version = "HTTP/1.1"
    counts = {}

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        model = "counted"
        _CountingUpstream.counts[model] = \
            _CountingUpstream.counts.get(model, 0) + 1
        payload = json.dumps({"choices": [{"delta": {"content": "hi"},
                                           "finish_reason": None}]}).encode()
        data = b"data: " + payload + b"\n\ndata: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestChat2ApiImageBudgetHttp(unittest.TestCase):
    """HTTP-level contract: the local current-turn 400 never reaches upstream.

    The module docstring promises exactly one such case - the whole point of
    the local reject is avoiding a pointless UM round trip (which the 502
    replay machinery would otherwise amplify).
    """

    def setUp(self):
        _CountingUpstream.counts = {}
        # B8-0 isolation lifecycle (see _chat2api_http_harness): the ledger
        # redirect is registered FIRST so it is released LAST - after every
        # handler drained and every server closed. The pre-fix LIFO bug
        # released the patch while a post-response handler was still writing
        # record_usage, which is how test rows reached the PRODUCTION
        # data/usage-chat2api.jsonl on 2026-10-02.
        self.tmp_dir, self.usage_log = harness.install_usage_isolation(
            self, chat2api)
        harness.assert_stub_credentials(
            chat2api, local_key=None, upstream_token=STUB_TOKEN,
            stub_token_literal=STUB_TOKEN)
        upstream = harness.TrackedHTTPServer(("127.0.0.1", 0),
                                             _CountingUpstream)
        self.upstream = upstream
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        harness.install_tracked_server(self, upstream, name="upstream-stub",
                                          require_drained=False)

        args = SimpleNamespace(
            base_url=f"http://127.0.0.1:{upstream.server_port}",
            token=STUB_TOKEN, profile="profile", login_timeout=1,
            use_api_key=False, no_browser=True)
        proxy = chat2api.Proxy({"token": STUB_TOKEN}, args)
        server = harness.TrackedHTTPServer(("127.0.0.1", 0), chat2api.Handler)
        server.proxy = proxy
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.server = server
        # Registered AFTER the redirect: this teardown (shutdown → bounded
        # drain of in-flight requests → close) runs BEFORE the USAGE_LOG
        # patch stop, so the post-response record_usage tail of the LAST
        # request lands in the temp ledger, never the production one.
        harness.install_tracked_server(self, server, name="chat2api-gateway")
        self.base = f"http://127.0.0.1:{server.server_port}"
        patcher = mock.patch.object(
            chat2api, "AUTO_TRIM_CURRENT_TURN_IMAGES", False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_current_turn_local_400_sends_zero_upstream_requests(self):
        images = [{"type": "image_url", "image_url": {"url": f"c-{i}"}}
                  for i in range(LIMIT + 1)]
        r = requests.post(
            f"{self.base}/v1/chat/completions",
            json={"model": "counted",
                  "messages": [{"role": "user",
                                "content": images + [{"type": "text",
                                                      "text": "too many"}]}],
                  "stream": False},
            timeout=10)
        self.assertEqual(r.status_code, 400)
        payload = r.json()
        self.assertEqual(payload["error"]["type"],
                         "too_many_images_current_turn")
        self.assertIn("Current turn contains", payload["error"]["message"])
        # The decisive contract: zero real upstream requests for a payload
        # the local normalizer already knows UM will reject.
        self.assertEqual(_CountingUpstream.counts.get("counted", 0), 0)


if __name__ == "__main__":
    unittest.main()
