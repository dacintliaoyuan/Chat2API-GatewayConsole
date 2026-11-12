# -*- coding: utf-8 -*-
"""Tests for console_log_reader.read_entries (run-log detail backend).

All fixtures live in per-test temporary directories; the chat2api service
log is redirected through the CHAT2API_SERVICE_LOG env override and the
ledger through ledger_paths injection, so no real production log is ever
read or written. Every secret used here is a fake value.
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

_READER_PATH = Path(__file__).resolve().parents[1] / "console/log_reader.py"
_SPEC = importlib.util.spec_from_file_location("console_log_reader_test",
                                               _READER_PATH)
console_log_reader = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = console_log_reader
_SPEC.loader.exec_module(console_log_reader)

read_entries = console_log_reader.read_entries
redact = console_log_reader.redact
_file_identity = console_log_reader._file_identity
MAX_TAIL_BYTES = console_log_reader.MAX_TAIL_BYTES
MAX_ROWS = console_log_reader.MAX_ROWS
_encode_cursor = console_log_reader._encode_cursor
MAX_LINE_BYTES = console_log_reader.MAX_LINE_BYTES
MAX_ROWS = console_log_reader.MAX_ROWS

OK_KEYS = {"status", "rows", "scanned_bytes", "window_start_offset",
           "truncated", "next_cursor", "reset_hint", "file_key"}

# "2026-09-21 10:00:00 INFO line-0000 " -> 35 bytes; fixed-width padding
# makes every generated line exactly ``width`` bytes plus one newline.
_TS_HEAD_LEN = 35


def _ts_line(index, width=200):
    head = "2026-09-21 10:%02d:%02d INFO line-%04d " % (
        (index // 60) % 60, index % 60, index)
    assert len(head) == _TS_HEAD_LEN
    return (head + "." * (width - len(head)) + "\n").encode("ascii")


@contextmanager
def _service_log(data):
    """Yield a temp file bound as the chat2api service log for the block."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chat2api.log"
        path.write_bytes(data)
        with mock.patch.dict(os.environ,
                             {"CHAT2API_SERVICE_LOG": str(path)}):
            yield path


class RedactUnitTests(unittest.TestCase):

    def test_secret_samples_all_redacted(self):
        # Contract: secret carriers are masked; fake values only.
        cases = [
            ("Authorization: Bearer abc123XYZ", ("abc123XYZ",),
             "Authorization: "),
            ("api_key=xyz", ("xyz",), "api_key="),
            ("password=hunter2", ("hunter2",), "password="),
            ("url?a=b&token=secret123", ("secret123",), "url?a=b&token="),
            ("sk-FAKEFAKE12345678", ("sk-FAKEFAKE12345678",), ""),
        ]
        for raw, secrets, kept_prefix in cases:
            with self.subTest(raw=raw):
                out = redact(raw)
                for secret in secrets:
                    self.assertNotIn(secret, out)
                self.assertIn("<redacted", out)
                self.assertIn(kept_prefix, out)
        html = '<div class="k">v &amp; w</div>'
        self.assertEqual(redact(html), html)

    def test_structured_and_header_carriers_shadowed(self):
        # P1 rework (directed review): bare token= regexes missed three
        # carriers (quoted JSON keys, multi-field Cookie headers, custom
        # headers). Sentinels must not survive; fake values only.
        cases = [
            ('{"token":"SYNTHETIC_SECRET_123"}', "SYNTHETIC_SECRET_123"),
            ('Cookie: a=1; b=2; c=3', "b=2; c=3"),
            ("Cookie: sid=1; auth=2; pref=dark", "pref=dark"),
            ("X-Admin-Control: SYNTHETIC-CTRL-99001", "SYNTHETIC-CTRL-99001"),
            ("X-Csrf-Token: SYNTHETIC-CSRF-88", "SYNTHETIC-CSRF-88"),
            ('"authorization": "SYNTH-QUOTE-77"', "SYNTH-QUOTE-77"),
        ]
        for raw, sentinel in cases:
            with self.subTest(raw=raw):
                out = redact(raw)
                self.assertNotIn(sentinel, out)
                self.assertIn("<redacted", out)


class ReadEntriesTests(unittest.TestCase):

    def test_request_identity_matches_event_not_substring_or_message(self):
        data = (b"2026-10-02 12:00:00 INFO request summary request=abc final=HTTP400\n"
                b"2026-10-02 12:00:01 INFO request summary request=abc2 final=ok\n"
                b"2026-10-02 12:00:02 INFO note about abc without an event identity\n")
        with _service_log(data):
            response = read_entries("chat2api", "service", request_id="abc")
        self.assertEqual(len(response["rows"]), 1)
        self.assertEqual(response["rows"][0]["event"]["request_id"], "abc")
        self.assertEqual(response["request_scope"], "read_window")
        self.assertEqual(response["scanned_bytes"], len(data))

    def test_request_identity_validation_and_bounded_empty_result(self):
        for invalid in ("", "a" * 65, "abc\n", "abc xyz", True):
            with self.subTest(invalid=invalid):
                self.assertEqual(read_entries("chat2api", "service", request_id=invalid)
                                 ["error"], "invalid_request_id")
        older = b"2026-10-02 12:00:00 INFO request summary request=older final=ok\n"
        newer = b"2026-10-02 12:00:01 INFO request summary request=newer final=ok\n"
        with _service_log(older + newer), mock.patch.object(console_log_reader, "MAX_TAIL_BYTES", len(newer)):
            response = read_entries("chat2api", "service", request_id="older")
        self.assertEqual(response["rows"], [])
        self.assertEqual(response["scanned_bytes"], len(newer))
        self.assertIsNotNone(response["next_cursor"])

    def test_plain_lines_parse_levels_and_shape(self):
        data = (b"2026-09-21 10:00:00 INFO boot ok\n"
                b"2026-09-21 10:00:01 WARN low disk\n"
                b"2026-09-21 10:00:02 ERR job failed\n")
        with _service_log(data) as path:
            resp = read_entries("chat2api", "service")
            self.assertEqual(resp["file_key"], _file_identity(path))
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(set(resp), OK_KEYS)
        self.assertEqual([r["level"] for r in resp["rows"]],
                         ["INFO", "WARNING", "ERROR"])
        self.assertEqual([r["message"] for r in resp["rows"]],
                         ["boot ok", "low disk", "job failed"])
        self.assertTrue(all(r["parse_ok"] for r in resp["rows"]))
        self.assertEqual(resp["rows"][0]["ts"], "2026-09-21 10:00:00")
        self.assertFalse(any(r["cut"] for r in resp["rows"]))
        self.assertFalse(resp["truncated"])
        self.assertFalse(resp["reset_hint"])
        self.assertIsNone(resp["next_cursor"])
        self.assertEqual(resp["window_start_offset"], 0)
        self.assertEqual(resp["scanned_bytes"], len(data))

    def test_traceback_continuation_lines_attach_to_previous_entry(self):
        data = (b"2026-09-21 10:00:00 ERROR job boom\n"
                b"Traceback (most recent call last):\n"
                b'  File "m.py", line 1, in run\n'
                b'    raise ValueError("boom")\n'
                b"ValueError: boom\n"
                b"\n"
                b"cleanup note\n"
                b"2026-09-21 10:00:05 INFO recovered\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        rows = resp["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["level"], "ERROR")
        self.assertTrue(rows[0]["parse_ok"])
        self.assertEqual(rows[0]["message"], "\n".join([
            "job boom",
            "Traceback (most recent call last):",
            '  File "m.py", line 1, in run',
            '    raise ValueError("boom")',
            "ValueError: boom",
            "",
            "cleanup note",
        ]))
        self.assertEqual(rows[1]["message"], "recovered")
        # Continuation content is searchable inside the parent message.
        with _service_log(data):
            q_rows = read_entries("chat2api", "service", q="ValueError")
        self.assertEqual(len(q_rows["rows"]), 1)

    def test_leading_unparsable_lines_kept_as_parse_ok_false(self):
        data = (b"stray line one\n"
                b"stray line two\n"
                b"2026-09-21 10:00:00 INFO after stray\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        rows = resp["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["parse_ok"], False)
        self.assertIsNone(rows[0]["ts"])
        self.assertIsNone(rows[0]["level"])
        self.assertEqual(rows[0]["message"], "stray line one\nstray line two")
        self.assertEqual(rows[1]["message"], "after stray")

    def test_missing_file_reports_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.log"
            with mock.patch.dict(os.environ,
                                 {"CHAT2API_SERVICE_LOG": str(path)}):
                resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "missing")
        self.assertEqual(resp["rows"], [])
        self.assertIsNone(resp["file_key"])
        self.assertIsNone(resp["next_cursor"])

    def test_empty_file_returns_empty_rows(self):
        with _service_log(b"") as path:
            resp = read_entries("chat2api", "service")
            self.assertEqual(resp["file_key"], _file_identity(path))
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["rows"], [])
        self.assertEqual(resp["scanned_bytes"], 0)
        self.assertEqual(resp["window_start_offset"], 0)
        self.assertFalse(resp["truncated"])
        self.assertIsNone(resp["next_cursor"])

    def test_unconfigured_and_unknown_sources(self):
        resp = read_entries("chat2api", "ledger")  # no ledger_paths injected
        self.assertEqual(resp["status"], "unconfigured")
        self.assertEqual(resp["rows"], [])
        resp = read_entries("nope", "nope")
        self.assertEqual(resp["status"], "error")
        self.assertEqual(resp["error"], "unknown_log_source")
        resp = read_entries("chat2api", "bogus-category")
        self.assertEqual(resp["error"], "unknown_log_source")
        resp = read_entries("console", "service")  # source has no categories
        self.assertEqual(resp["status"], "error")
        self.assertEqual(set(resp), OK_KEYS | {"error"})
        self.assertEqual(resp["rows"], [])

    def test_share_backend_registry_binds_dedicated_paths(self):
        """The 8110 dedicated instance's logs resolve from its deployment dir,
        never from the 8000 main-chain files. Every read stays inside the env
        patch: the reader re-resolves the registry per call, and an unpatched
        read would silently hit the REAL deployment directory."""
        with tempfile.TemporaryDirectory() as tmp:
            service = Path(tmp) / "share-backend.log"
            service.write_bytes(_ts_line(0))
            ledger = Path(tmp) / "usage-share-backend.jsonl"
            ledger.write_bytes(b'{"time":"2026-09-22T10:00:00","model":"m"}\n')
            with mock.patch.dict(os.environ,
                                 {"CONSOLE_SHARE_BACKEND_DIR": tmp}):
                registry = console_log_reader.build_registry({"chat2api": ""})
                share = next(
                    s for s in registry["sources"] if s["source"] == "share")
                by_id = {c["category"]: c for c in share["categories"]}
                self.assertEqual(by_id["service"]["path"], str(service))
                self.assertTrue(by_id["service"]["rotating"])
                self.assertEqual(by_id["ledger"]["path"], str(ledger))
                resp = read_entries("share", "service")
                self.assertEqual(resp["status"], "ok")
                self.assertTrue(
                    any("line-0000" in r["message"] for r in resp["rows"]))
                resp = read_entries("share", "ledger")
                self.assertEqual(resp["status"], "ok")
                # Ledger JSONL rows keep parse_ok False (no level invented)
                # but carry a real ts row, same as the chat2api ledger.
                self.assertEqual(resp["rows"][0]["parse_ok"], False)
                self.assertIn("model=m", resp["rows"][0]["message"])

    def test_share_backend_registry_honest_when_unresolvable(self):
        """No deployment dir -> every share category stays registered; the ones
        that need the deployment dir have path=None (unconfigured), while the
        fixed SYSTEM-run artifacts keep their paths. A missing producer is never
        folded into 'no records' and never silently dropped."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONSOLE_SHARE_BACKEND_DIR", None)
            os.environ.pop("LOCALAPPDATA", None)
            registry = console_log_reader.build_registry({})
            share = next(s for s in registry["sources"] if s["source"] == "share")
            by_id = {c["category"]: c for c in share["categories"]}
            self.assertIsNone(by_id["service"]["path"])
            self.assertIsNone(by_id["ledger"]["path"])
            self.assertIsNone(by_id["lifecycle"]["path"])
            self.assertIsNone(by_id["watch"]["path"])
            self.assertIsNotNone(by_id["gateway"]["path"])
            self.assertIsNotNone(by_id["faults"]["path"])
            self.assertIsNotNone(by_id["broker"]["path"])
            resp = read_entries("share", "service")
        self.assertEqual(resp["status"], "unconfigured")
        self.assertEqual(resp["rows"], [])

    def test_share_backend_env_override_must_be_absolute(self):
        with mock.patch.dict(os.environ,
                             {"CONSOLE_SHARE_BACKEND_DIR": "relative\\path"}):
            registry = console_log_reader.build_registry({})
        share = next(s for s in registry["sources"] if s["source"] == "share")
        by_id = {c["category"]: c for c in share["categories"]}
        self.assertIsNone(by_id["service"]["path"])
        self.assertIsNone(by_id["ledger"]["path"])
        self.assertIsNotNone(by_id["broker"]["path"])

    def test_overlong_line_is_truncated_with_cut_flag(self):
        data = (b"2026-09-21 10:00:00 INFO " + b"A" * 9000 + b"\n"
                b"2026-09-21 10:00:01 INFO short\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        rows = resp["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["cut"])
        self.assertEqual(len(rows[0]["message"]), MAX_LINE_BYTES - 25)
        self.assertFalse(rows[1]["cut"])
        self.assertEqual(rows[1]["message"], "short")

    def test_invalid_utf8_bytes_do_not_crash(self):
        emoji = "\U0001f984".encode("utf-8")  # 4-byte char, overlong line
        data = (b"2026-09-21 10:00:00 INFO caf\xe9 open\n"
                b"2026-09-21 10:00:01 INFO " + emoji * 3000 + b"\n"
                b"2026-09-21 10:00:02 INFO fine\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "ok")
        rows = resp["rows"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["parse_ok"])
        self.assertIn("\ufffd", rows[0]["message"])
        self.assertTrue(rows[1]["cut"])
        # MAX_LINE_BYTES caps bytes; decoded emoji chars are 4 bytes each,
        # so the kept message has far fewer characters than the cap.
        self.assertLess(len(rows[1]["message"]), MAX_LINE_BYTES)
        self.assertIn("\ufffd", rows[1]["message"])  # split emoji char
        self.assertEqual(rows[2]["message"], "fine")

    def test_forged_and_out_of_range_cursors_reset(self):
        data = (b"2026-09-21 10:00:00 INFO one\n"
                b"2026-09-21 10:00:01 INFO two\n")
        with _service_log(data) as path:
            key = _file_identity(path)
            size = len(data)
            def raw_cursor(payload):
                return base64.b64encode(
                    json.dumps(payload).encode("utf-8")).decode("ascii")

            # _encode_cursor coerces off with int(), so cursors that must be
            # rejected at decode level (negative / bool / string offset) are
            # built by hand here.
            bad_cursors = [
                "garbage!!",
                base64.b64encode(b"not json").decode("ascii"),
                base64.b64encode(b"[1, 2, 3]").decode("ascii"),
                raw_cursor({"src": "chat2api", "cat": "service", "off": -1,
                            "key": key}),
                raw_cursor({"src": "chat2api", "cat": "service", "off": True,
                            "key": key}),
                raw_cursor({"src": "chat2api", "cat": "service", "off": "9",
                            "key": key}),
                _encode_cursor(5, "service", 1, key),
                _encode_cursor("chat2api", "service", size + 50, key),
                _encode_cursor("chat2api", "service", size, "forged-key"),
                _encode_cursor("qoder_cn", "requests", 1, key),
            ]
            for forged in bad_cursors:
                with self.subTest(cursor=forged):
                    resp = read_entries("chat2api", "service", cursor=forged)
                    self.assertEqual(resp["status"], "ok")
                    self.assertEqual(resp["rows"], [])
                    self.assertTrue(resp["reset_hint"])
                    self.assertIsNone(resp["next_cursor"])
                    self.assertEqual(resp["file_key"], key)

    def test_rotation_changes_file_key_and_resets(self):
        v1 = (b"2026-09-21 10:00:00 INFO gen one line a\n"
              b"2026-09-21 10:00:01 INFO gen one line b\n")
        with _service_log(v1) as path:
            first = read_entries("chat2api", "service")
            old_key = first["file_key"]
            self.assertFalse(first["reset_hint"])
            v2 = b"2026-09-21 10:00:02 INFO gen two replacement\n"
            path.write_bytes(v2)
            new_key = _file_identity(path)
            self.assertNotEqual(old_key, new_key)
            stale = _encode_cursor("chat2api", "service", len(v1), old_key)
            resp = read_entries("chat2api", "service", cursor=stale)
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(resp["rows"], [])
            self.assertTrue(resp["reset_hint"])
            self.assertEqual(resp["file_key"], new_key)

    def test_same_name_bigger_replacement_resets(self):
        # P1 rework (directed review): a same-named LARGER file (more lines,
        # new inode via os.replace) must not be read through the old cursor
        # as if it were an append — the "growth tolerance" verdict only
        # covers same-inode growth.
        old = b"".join(_ts_line(i, width=500) for i in range(600))
        new = b"".join(_ts_line(i, width=600) for i in range(650))
        assert len(new) > len(old)
        with _service_log(old) as path:
            tail = read_entries("chat2api", "service")
            anchor = tail["next_cursor"]
            self.assertIsNotNone(anchor)
            new_path = path.with_name("chat2api.new")
            new_path.write_bytes(new)
            os.replace(new_path, path)
            resp = read_entries("chat2api", "service", cursor=anchor)
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(resp["rows"], [])
            self.assertTrue(resp["reset_hint"])

    def test_tail_read_never_exceeds_snapshot_budget(self):
        # 450 x 601 = 270450 > MAX_TAIL_BYTES: the reader must consume at
        # most MAX_TAIL_BYTES for one tail request even if the file grows.
        data = b"".join(_ts_line(i, width=600) for i in range(450))
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "ok")
        self.assertLessEqual(resp["scanned_bytes"], MAX_TAIL_BYTES)
        self.assertEqual(len(resp["rows"]), MAX_ROWS)

    def test_rotation_generation_whitelist_and_fallback(self):
        # Slice 2 (directed review): rotation stays a whitelist index.
        # Generation 1 reads chat2api.log.1; out-of-cap or non-rotating
        # usage answers invalid_rotation; a missing generation falls back
        # to the active path instead of inventing content.
        active = _ts_line(0, width=200)
        with _service_log(active) as path:
            rotated = path.with_name(f"{path.name}.1")
            rotated.write_bytes(b"2026-09-21 09:00:00 INFO rotated gen one\n")
            hit = read_entries("chat2api", "service", rotation=1)
            self.assertEqual(hit["status"], "ok")
            self.assertIn("rotated gen one", hit["rows"][0]["message"])
            beyond = read_entries("chat2api", "service", rotation=9)
            self.assertEqual(beyond["status"], "error")
            self.assertEqual(beyond["error"], "invalid_rotation")
            zero = read_entries("chat2api", "service", rotation=0)
            self.assertEqual(zero["status"], "error")
            self.assertEqual(zero["error"], "invalid_rotation")
            # Rotation generation 4 does not exist: fall back to the
            # active path, never invent content.
            missing_gen = read_entries("chat2api", "service", rotation=4)
            self.assertEqual(missing_gen["status"], "ok")
            self.assertIn("line-0000", missing_gen["rows"][0]["message"])

    def test_ledger_jsonl_lines_become_rows(self):
        # Slice 2 (directed review): call-ledger JSONL rows become real
        # timestamped rows (parse_ok stays False — no level invented), so
        # time filtering works and each call is one row, not a merged blob.
        rows = [
            '{"time": "2026-09-21T18:01:00", "model": "GLM-5.3-Flash", "status": "ok", "input_tokens": 120, "output_tokens": 30, "duration_ms": 1200}',
            '{"time": "2026-09-21T18:02:00", "model": "GLM-5.2", "status": "failed", "credits": 1.5}',
            "not json at all\n",
        ]
        data = ("\n".join(rows) + "\n").encode("utf-8")
        with _service_log(data):
            resp = read_entries("chat2api", "service", since="2026-09-21T18:02")
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(len(resp["rows"]), 1)
            self.assertIn("model=GLM-5.2", resp["rows"][0]["message"])
            self.assertIn("credits=1.5", resp["rows"][0]["message"])
            self.assertFalse(resp["rows"][0]["parse_ok"])
            nofilter = read_entries("chat2api", "service")
            self.assertEqual(len([r for r in nofilter["rows"] if r["ts"]]), 2)
            # The non-JSON line follows a JSON row: the continuation
            # verdict keeps it attached to the entry above (context kept).
            last_row = nofilter["rows"][-1]
            self.assertIn("not json at all", last_row["message"])

    def test_tail_row_budget_keeps_newest_max_rows(self):
        # 450 lines x 601 bytes = 270450 > MAX_TAIL_BYTES; the tail window
        # starts mid-line 13 and aligns to 14*601 = 8414, so 436 lines are
        # parsed and the row budget keeps the newest 400 (lines 50..449).
        data = b"".join(_ts_line(i, width=600) for i in range(450))
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(len(resp["rows"]), MAX_ROWS)
        self.assertTrue(resp["truncated"])
        self.assertIsNotNone(resp["next_cursor"])
        self.assertEqual(resp["window_start_offset"], 8414)
        self.assertIn("line-0050", resp["rows"][0]["message"])
        self.assertIn("line-0449", resp["rows"][-1]["message"])
        self.assertLess(resp["rows"][0]["ts"], resp["rows"][-1]["ts"])

    def test_tail_window_honesty_when_filters_match_nothing(self):
        # 300 lines x 1001 bytes = 300300 > MAX_TAIL_BYTES; the tail window
        # starts mid-line 38 and aligns to 39*1001 = 39039. With a keyword
        # that matches nothing the response must still say where coverage
        # started instead of pretending the file is empty.
        data = b"".join(_ts_line(i, width=1000) for i in range(300))
        with _service_log(data):
            hit = read_entries("chat2api", "service", q="line-02")
            self.assertEqual(len(hit["rows"]), 100)
            self.assertIn("line-0200", hit["rows"][0]["message"])
            self.assertIn("line-0299", hit["rows"][-1]["message"])
            self.assertFalse(hit["truncated"])
            miss = read_entries("chat2api", "service", q="absent-marker")
            self.assertEqual(miss["status"], "ok")
            self.assertEqual(miss["rows"], [])
            self.assertEqual(miss["window_start_offset"], 39039)
            self.assertFalse(miss["truncated"])
            # R9-R38 verdict: the tail response mints its paging anchor at
            # the window start, so the viewer can load earlier pages even
            # when this window matches nothing.
            self.assertIsNotNone(miss["next_cursor"])

    def test_older_paging_walks_back_with_cursor(self):
        # 884 lines x 1001 bytes; line j starts at 1001*j. The tail window
        # starts mid-line 622 and aligns to 623*1001 = 623623 (261 rows).
        # Each older page covers exactly [off-262144, off), so the chain
        # tail -> page1 -> page2 -> page3 tiles the file with no overlap
        # and no gap.
        data = b"".join(_ts_line(i, width=1000) for i in range(884))
        with _service_log(data) as path:
            tail = read_entries("chat2api", "service")
            ws = tail["window_start_offset"]
            self.assertEqual(ws, 623623)
            self.assertEqual(len(tail["rows"]), 261)
            self.assertIn("line-0623", tail["rows"][0]["message"])
            self.assertIn("line-0883", tail["rows"][-1]["message"])
            key = tail["file_key"]
            bootstrap = _encode_cursor("chat2api", "service", ws, key)
            page1 = read_entries("chat2api", "service", cursor=bootstrap)
            self.assertEqual(len(page1["rows"]), 261)
            self.assertIn("line-0362", page1["rows"][0]["message"])
            self.assertIn("line-0622", page1["rows"][-1]["message"])
            self.assertEqual(page1["window_start_offset"], 362362)
            self.assertEqual(
                page1["next_cursor"],
                _encode_cursor("chat2api", "service", 362362, key))
            self.assertFalse(page1["reset_hint"])
            page2 = read_entries("chat2api", "service",
                                 cursor=page1["next_cursor"])
            self.assertEqual(len(page2["rows"]), 261)
            self.assertIn("line-0101", page2["rows"][0]["message"])
            self.assertIn("line-0361", page2["rows"][-1]["message"])
            self.assertEqual(
                page2["next_cursor"],
                _encode_cursor("chat2api", "service", 101101, key))
            page3 = read_entries("chat2api", "service",
                                 cursor=page2["next_cursor"])
            self.assertEqual(len(page3["rows"]), 101)
            self.assertIn("line-0000", page3["rows"][0]["message"])
            self.assertIn("line-0100", page3["rows"][-1]["message"])
            self.assertEqual(page3["window_start_offset"], 0)
            self.assertIsNone(page3["next_cursor"])
            self.assertFalse(page3["truncated"])
            self.assertFalse(page3["reset_hint"])

    def test_older_row_budget_keeps_newest_and_mints_cursor(self):
        # 900 lines x 601 bytes = 540900; a cursor at EOF reads the window
        # [278864, 540900) after alignment -> 436 lines, trimmed to the
        # newest 400 (lines 500..899) with next_cursor anchored at 500*601.
        data = b"".join(_ts_line(i, width=600) for i in range(900))
        with _service_log(data) as path:
            key = _file_identity(path)
            cursor = _encode_cursor("chat2api", "service", len(data), key)
            resp = read_entries("chat2api", "service", cursor=cursor)
            self.assertEqual(resp["status"], "ok")
            self.assertEqual(len(resp["rows"]), MAX_ROWS)
            self.assertTrue(resp["truncated"])
            self.assertEqual(resp["window_start_offset"], 278864)
            self.assertIn("line-0500", resp["rows"][0]["message"])
            self.assertIn("line-0899", resp["rows"][-1]["message"])
            self.assertEqual(
                resp["next_cursor"],
                _encode_cursor("chat2api", "service", 300500, key))
            page2 = read_entries("chat2api", "service",
                                 cursor=resp["next_cursor"])
            self.assertEqual(len(page2["rows"]), MAX_ROWS)
            self.assertIn("line-0100", page2["rows"][0]["message"])
            self.assertIn("line-0499", page2["rows"][-1]["message"])
            page3 = read_entries("chat2api", "service",
                                 cursor=page2["next_cursor"])
            self.assertEqual(len(page3["rows"]), 100)
            self.assertIn("line-0000", page3["rows"][0]["message"])
            self.assertIn("line-0099", page3["rows"][-1]["message"])
            self.assertIsNone(page3["next_cursor"])

    def test_filters_keyword_level_and_time_bounds(self):
        data = (b"2026-09-21 10:00:00 INFO alpha request ok\n"
                b"2026-09-21 10:00:02 WARNING disk almost full\n"
                b"2026-09-21 10:00:03 ERROR alpha pipeline failed\n"
                b"2026-09-21 10:00:05 INFO beta done\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage-chat2api.jsonl"
            path.write_bytes(data)
            lp = {"chat2api": str(path)}

            q_rows = read_entries("chat2api", "ledger", q="ALPHA",
                                  ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in q_rows],
                             ["2026-09-21 10:00:00", "2026-09-21 10:00:03"])

            lvl_rows = read_entries("chat2api", "ledger", level="warn",
                                    ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in lvl_rows],
                             ["2026-09-21 10:00:02"])

            err_rows = read_entries("chat2api", "ledger", level="err",
                                    ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in err_rows],
                             ["2026-09-21 10:00:03"])

            since_rows = read_entries("chat2api", "ledger",
                                      since="2026-09-21 10:00:03",
                                      ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in since_rows],
                             ["2026-09-21 10:00:03", "2026-09-21 10:00:05"])

            until_rows = read_entries("chat2api", "ledger",
                                      until="2026-09-21 10:00:03",
                                      ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in until_rows],
                             ["2026-09-21 10:00:00", "2026-09-21 10:00:02",
                              "2026-09-21 10:00:03"])

            day_rows = read_entries("chat2api", "ledger",
                                    until="2026-09-21",
                                    ledger_paths=lp)["rows"]
            self.assertEqual(len(day_rows), 4)

            combo_rows = read_entries(
                "chat2api", "ledger", since="2026-09-21 10:00:02",
                until="2026-09-21 10:00:03", ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in combo_rows],
                             ["2026-09-21 10:00:02", "2026-09-21 10:00:03"])

            both_rows = read_entries("chat2api", "ledger", q="alpha",
                                     level="error", ledger_paths=lp)["rows"]
            self.assertEqual([r["ts"] for r in both_rows],
                             ["2026-09-21 10:00:03"])

            resp = read_entries("chat2api", "ledger", ledger_paths=lp)
            self.assertEqual(resp["file_key"], _file_identity(path))
            self.assertEqual(set(resp), OK_KEYS)

    def test_query_filter_no_match_reports_honest_window(self):
        # 300 lines x 1001 bytes = 300300 > MAX_TAIL_BYTES; the tail window
        # starts mid-line 38 and aligns to 39*1001 = 39039 (261 rows in
        # window). No match still reports the honest window bounds.
        data = b"".join(_ts_line(i, width=1000) for i in range(300))
        with _service_log(data):
            miss = read_entries("chat2api", "service", q="absent-marker")
            self.assertEqual(miss["status"], "ok")
            self.assertEqual(miss["rows"], [])
            self.assertEqual(miss["window_start_offset"], 39039)
            self.assertFalse(miss["truncated"])
            # R9-R38 verdict: the tail response mints its paging anchor at
            # the window start, so the viewer can load earlier pages even
            # when this window matches nothing.
            self.assertIsNotNone(miss["next_cursor"])

    def test_file_without_final_newline_flags_truncated(self):
        data = (b"2026-09-21 10:00:00 INFO complete one\n"
                b"2026-09-21 10:00:01 INFO partial tail")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        self.assertEqual(len(resp["rows"]), 1)
        self.assertEqual(resp["rows"][0]["message"], "complete one")
        self.assertTrue(resp["truncated"])
        solo = b"2026-09-21 10:00:00 INFO solo"
        with _service_log(solo):
            resp = read_entries("chat2api", "service")
        self.assertEqual(resp["rows"], [])
        self.assertTrue(resp["truncated"])

    def test_wall_clock_budget_stops_scan(self):
        data = b"".join(_ts_line(i, width=200) for i in range(300))

        class _FakeClock:
            def __init__(self, seq):
                self._seq = iter(seq)

            def monotonic(self):
                return next(self._seq)

        with _service_log(data):
            # Readings: deadline setup, first line check, second line check.
            clock = _FakeClock([0.0, 0.5, 100.0, 100.0])
            with mock.patch.object(console_log_reader, "time", clock):
                resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "ok")
        self.assertTrue(resp["truncated"])
        self.assertEqual(len(resp["rows"]), 1)
        self.assertIn("line-0000", resp["rows"][0]["message"])


    def test_secret_samples_redacted_in_rows(self):
        data = (b"2026-09-21 10:00:00 INFO Authorization: Bearer abc123XYZ "
                b"api_key=xyz password=hunter2\n"
                b"2026-09-21 10:00:01 INFO GET https://x.test/?a=b"
                b"&token=secret123\n"
                b"2026-09-21 10:00:02 INFO key sk-FAKEFAKE12345678\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        rows = resp["rows"]
        self.assertEqual(len(rows), 3)
        joined = "\n".join(r["message"] for r in rows)
        for secret in ("abc123XYZ", "hunter2", "secret123",
                       "sk-FAKEFAKE12345678"):
            self.assertNotIn(secret, joined)
        self.assertIn("<redacted>", joined)
        self.assertIn("<redacted-key>", joined)

    def test_html_text_passes_through_unchanged(self):
        line = (b'2026-09-21 10:00:00 INFO <div class="c">a &amp; b</div>\n')
        with _service_log(line):
            resp = read_entries("chat2api", "service")
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["rows"][0]["message"],
                         '<div class="c">a &amp; b</div>')
        self.assertTrue(resp["rows"][0]["parse_ok"])


class RunEventFieldTests(unittest.TestCase):
    """Rev8 B8-5：统一运行事件字段（加性、先脱敏、不可证明即 null）。

    账本 JSONL 行携带结构化事件（timestamp/model/request_id/phase/outcome/
    error_code/duration_ms/token_rate）；运行日志行只提取受控键；无法解析
    的行事件字段全 None + 脱敏原文，绝不虚构。
    """

    def _read(self, data, source="chat2api", category="service"):
        with _service_log(data):
            return console_log_reader.read_entries(source, category)

    def test_ledger_line_carries_structured_event(self):
        line = json.dumps({
            "time": "2026-10-02T12:00:00+08:00", "service": "chat2api",
            "model": "GLM-5.3-Flash", "stream": True, "status": "ok",
            "input_tokens": 100, "output_tokens": 900,
            "duration_ms": 3000,
        }, ensure_ascii=False) + "\n"
        resp = self._read(line.encode("utf-8"))
        self.assertEqual(resp["status"], "ok")
        event = resp["rows"][0]["event"]
        self.assertEqual(event["timestamp"], "2026-10-02T12:00:00+08:00")
        self.assertEqual(event["model"], "GLM-5.3-Flash")
        self.assertEqual(event["outcome"], "ok")
        self.assertIsNone(event["error_code"])
        self.assertEqual(event["duration_ms"], 3000)
        # token_rate = 900 output tokens / 3.0 s = 300 tok/s，kind 标
        # mean_overall（含首字等待的整次平均）。
        self.assertEqual(event["token_rate"], 300.0)
        self.assertEqual(event["token_rate_kind"], "mean_overall")

    def test_error_ledger_row_carries_error_code_and_no_rate(self):
        line = json.dumps({
            "time": "2026-10-02T12:00:01+08:00", "model": "m",
            "status": "HTTP400", "output_tokens": 0, "duration_ms": 1200,
        }) + "\n"
        resp = self._read(line.encode("utf-8"))
        event = resp["rows"][0]["event"]
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["error_code"], "HTTP400")
        # 可信 0 输出不是未知：0/1.2s = 0 tok/s 是合法平均，kind 仍标。
        self.assertEqual(event["token_rate"], 0.0)
        self.assertEqual(event["token_rate_kind"], "mean_overall")

    def test_ts_line_extracts_controlled_tokens_only(self):
        data = (b"2026-10-02 12:00:02 WARNING chat2api: upstream_reject "
                b"request=r-1 status=400 final=HTTP400 model=m-a "
                b"duration_ms=1500 payload-here\n")
        resp = self._read(data)
        event = resp["rows"][0]["event"]
        self.assertEqual(event["timestamp"], "2026-10-02 12:00:02")
        self.assertEqual(event["request_id"], "r-1")
        self.assertEqual(event["model"], "m-a")
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["error_code"], "HTTP400")
        self.assertEqual(event["duration_ms"], 1500)
        self.assertIsNone(event["token_rate"])

    def test_unparsable_line_event_fields_all_null(self):
        resp = self._read(b"totally unparsable noise line\n")
        row = resp["rows"][0]
        self.assertFalse(row["parse_ok"])
        self.assertEqual(
            row["event"],
            {"timestamp": None, "model": None, "request_id": None,
             "phase": None, "outcome": None, "error_code": None,
             "duration_ms": None, "token_rate": None})


def _source(registry, source_id):
    for src in registry["sources"]:
        if src.get("source") == source_id:
            return src
    raise AssertionError("source not registered: %s" % source_id)


class RegistryInventoryTests(unittest.TestCase):
    """P3/R3：registry 按实际启动器 / 监督入口逐项盘点，不按目录递归。

    每个断言都对着本会话实际读到的生产者：
    - chat2api 隐藏启动器 start_chat2api.bat 写 chat2api.console.log /
      .prev，监督 lib/watch-chat2api.ps1 写 watch/chat2api-watch.log；
    - 分享链：8110 父侧 backend-lifecycle.log、监督
      share-backend-watch.log、8100 raw.txt 与
      share-faults.jsonl、broker lifecycle.json。
    - B0（2026-11-01）：Qoder 国内/国际与 CLIProxy 已退役，registry
      不再登记它们的来源（/api/console/log-sources 与 log-entries 不再出现）。
    """

    def test_retired_sources_are_not_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(console_log_reader, "_HERE", root):
                registry = console_log_reader.build_registry({})
            ids = {src["source"] for src in registry["sources"]}
            self.assertEqual(ids, {"chat2api", "console", "share"})
            for retired in ("qoder_cn", "qoder_global", "gemini"):
                self.assertNotIn(retired, ids)
                resp = read_entries(retired, "service")
                self.assertEqual(resp["status"], "error")
                self.assertEqual(resp["error"], "unknown_log_source")
                self.assertEqual(resp["rows"], [])

    def test_chat2api_console_prev_and_watch_are_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with _overview_env(root):
                registry = console_log_reader.build_registry(
                    {"chat2api": str(root / "ledger.jsonl")})
            cats = {c["category"]: c
                    for c in _source(registry, "chat2api")["categories"]}
            self.assertEqual(cats["console"]["path"],
                             str(root / "chat2api" / "chat2api.console.log"))
            self.assertEqual(
                cats["console_prev"]["path"],
                str(root / "chat2api" / "chat2api.console.prev.log"))
            self.assertEqual(
                cats["watch"]["path"],
                str(root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
                    / "watch" / "chat2api-watch.log"))
            self.assertTrue(cats["watch"]["rotating"])
            self.assertEqual(cats["watch"]["rotation_index"], 1)

    def test_share_registry_covers_gateway_broker_supervision_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_root = root / "UMShareRun"
            with _overview_env(root):
                registry = console_log_reader.build_registry({})
            cats = {c["category"]: c
                    for c in _source(registry, "share")["categories"]}
            self.assertEqual(
                cats["lifecycle"]["path"],
                str(root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
                    / "share-backend" / "backend-lifecycle.log"))
            self.assertEqual(
                cats["watch"]["path"],
                str(root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
                    / "watch" / "share-backend-watch.log"))
            self.assertEqual(cats["watch"]["rotation_index"], 1)
            self.assertEqual(cats["gateway"]["path"],
                             str(run_root / "run" / "live" / "raw.txt"))
            self.assertEqual(cats["faults"]["path"],
                             str(run_root / "run" / "live"
                                 / "share-faults.jsonl"))
            self.assertEqual(cats["broker"]["path"],
                             str(run_root / "broker" / "lifecycle.json"))

    def test_console_source_explains_the_absent_producer(self):
        """8001 宿主不落盘任何行日志（start_usage_service.bat 无重定向）：
        来源保持注册但零目标，并用 note 说明原因——空列表不是静默缺口。"""
        registry = console_log_reader.build_registry({})
        console = _source(registry, "console")
        self.assertEqual(console["categories"], [])
        self.assertTrue(console.get("note"))
        self.assertIn("8001", console["note"])
        self.assertNotIn("8096", console["note"])

    def test_whole_document_target_reads_one_record(self):
        """broker lifecycle.json 由 tmp + replace 原子写成单个 JSON 文档
        （无行日志、无结尾换行）：整文档读取为一条记录，不是 0 行。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_root = root / "UMShareRun"
            (run_root / "broker").mkdir(parents=True)
            (run_root / "broker" / "lifecycle.json").write_text(
                '{"state": "stopped", "at": 1791348801, "by": "emergency-stop"}',
                encoding="utf-8")
            with _overview_env(root):
                result = console_log_reader.read_overview({})
            targets = {(t["source"], t["category"]): t
                       for t in result["targets"]}
            broker = targets[("share", "broker")]
            self.assertEqual(broker["status"], "ok")
            self.assertEqual(broker["rows"], 1)
            self.assertFalse(broker["truncated"])
            row = next(r for r in result["rows"]
                        if r["source"] == "share" and r["category"] == "broker")
            self.assertEqual(row["ts"], "1791348801")
            self.assertEqual(row["ts_norm"], 1791348801.0)
            self.assertIn('"state": "stopped"', row["message"])
            self.assertIn("emergency-stop", row["message"])
            # 整文档目标没有分页：传游标如实回 reset_hint。
            resp = console_log_reader.read_entries(
                "share", "broker",
                cursor=console_log_reader._encode_cursor("share", "broker", 1,
                                                         "key"))
            self.assertTrue(resp["reset_hint"])
            self.assertEqual(resp["rows"], [])

    def test_watch_root_requires_an_absolute_override(self):
        with mock.patch.dict(os.environ,
                             {"SELFUSEAI_GATEWAY_WATCH_DIR": "relative\\path"}):
            registry = console_log_reader.build_registry({})
            chat = {c["category"]: c
                    for c in _source(registry, "chat2api")["categories"]}
            share = {c["category"]: c
                     for c in _source(registry, "share")["categories"]}
            self.assertIsNone(chat["watch"]["path"])
            self.assertIsNone(share["watch"]["path"])
            resp = read_entries("chat2api", "watch")
        self.assertEqual(resp["status"], "unconfigured")


class TimeNormalizationTests(unittest.TestCase):
    """R3：归一时间用于跨来源排序；保留原始文本与未知时间状态。"""

    def test_normalize_ts_accepts_producer_formats(self):
        normalize = console_log_reader.normalize_ts
        # 已知向量（独立算得：datetime(..., tzinfo=+08:00).timestamp()）。
        self.assertEqual(normalize("2026-09-21T02:00:00+08:00"), 1789927200.0)
        self.assertEqual(normalize("2026-09-21T22:00:00+08:00"), 1789999200.0)
        self.assertEqual(normalize("2026-09-21 22:00:00+08:00"), 1789999200.0)
        self.assertEqual(normalize("2026-09-22T22:39:14+08:00"), 1790087954.0)
        self.assertEqual(normalize("2026-10-07 周三 15:20:42.65+08:00"),
                         1791357642.65)
        self.assertEqual(normalize("2026/09/22 22:39:14+08:00"), 1790087954.0)
        self.assertEqual(normalize("2026-09-21T02:00:00Z"), 1789956000.0)
        self.assertEqual(normalize("2026-09-21 22:00:00,945+08:00"),
                         1789999200.945)
        # broker / 监督记录把 at 写成整数 epoch 秒。
        self.assertEqual(normalize("1791348801"), 1791348801.0)
        # 无时区声明按本机本地时间（生产者写本地时间），仅要求可解析。
        self.assertIsNotNone(normalize("2026-09-21 22:00:00"))
        self.assertIsNotNone(normalize("2026-10-07 周三 15:20:42.65"))

    def test_normalize_ts_rejects_unknown_times(self):
        normalize = console_log_reader.normalize_ts
        for value in (None, "", "not a time", "2026-09-21", 12345,
                      "2026-13-40 99:99:99", True):
            with self.subTest(value=value):
                self.assertIsNone(normalize(value))

    def test_native_go_lines_parse_time_without_level(self):
        data = (b"2026/09/22 22:39:14 listening on 127.0.0.1:8097\n"
                b"2026/09/22 22:39:32 req a5de93c1 chat model=dfmodel stream=false\n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        rows = resp["rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ts"], "2026/09/22 22:39:14")
        self.assertEqual(rows[0]["message"], "listening on 127.0.0.1:8097")
        # 无 level 不虚构：parse_ok=False，但时间真实。
        self.assertFalse(rows[0]["parse_ok"])
        self.assertIsNone(rows[0]["level"])
        self.assertIsNotNone(console_log_reader.normalize_ts(rows[0]["ts"]))

    def test_launcher_tag_and_weekday_lines_keep_the_tag(self):
        data = (b"[launcher] 2026/10/07 \xe5\x91\xa8\xe4\xb8\x89 15:20:42.65 "
                b"starting hidden \n")
        with _service_log(data):
            resp = read_entries("chat2api", "service")
        row = resp["rows"][0]
        self.assertEqual(row["ts"], "2026/10/07 周三 15:20:42.65")
        self.assertTrue(row["message"].startswith("[launcher] starting hidden"))
        self.assertFalse(row["parse_ok"])
        self.assertIsNotNone(console_log_reader.normalize_ts(row["ts"]))


@contextmanager
def _overview_env(root):
    """Sandbox the reader's three path roots for one overview test.

    _HERE, SHARE_RUN_ROOT and LOCALAPPDATA all point into the temp root, so
    no real production log (repo paths, %LOCALAPPDATA% or the SYSTEM share
    run root) is ever read.
    """
    with mock.patch.object(console_log_reader, "_HERE", root), \
         mock.patch.object(console_log_reader, "SHARE_RUN_ROOT",
                           root / "UMShareRun"), \
         mock.patch.dict(os.environ,
                          {"LOCALAPPDATA": str(root / "LocalAppData")}):
        yield


class OverviewTests(unittest.TestCase):
    """P3/R3：聚合所有目标类别（不再首分类截断），每目标独立游标，
    归一时间排序，汇总如实标明覆盖 / 截断 / 未读 / 失败。"""

    def _tree(self, tmp):
        root = Path(tmp)
        (root / "chat2api").mkdir(parents=True, exist_ok=True)
        (root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
         / "share-backend").mkdir(parents=True, exist_ok=True)
        return root

    def _share_service(self, root):
        return (root / "LocalAppData" / "SelfUseAI" / "ai-gateway"
                / "share-backend" / "share-backend.log")

    def test_overview_reads_every_target_not_only_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "chat2api" / "chat2api.log").write_bytes(
                b"2026-09-21 22:00:00 INFO main service line\n")
            (root / "chat2api" / "chat2api.console.log").write_bytes(
                b"2026-09-21 22:01:00 INFO console line\n")
            self._share_service(root).write_bytes(
                b'{"time": "2026-09-21T22:02:00+08:00", "model": "m"}\n')
            with _overview_env(root):
                result = console_log_reader.read_overview(
                    ledger_paths={"chat2api": str(root / "ledger.jsonl")})
            messages = "\n".join(row["message"] for row in result["rows"])
            # 第二分类（console）与另一来源（share）都在，不是首分类截断。
            self.assertIn("console line", messages)
            self.assertIn("model=m", messages)
            self.assertIn("main service line", messages)
            cats = {(row["source"], row["category"]) for row in result["rows"]}
            self.assertIn(("chat2api", "console"), cats)
            self.assertIn(("share", "service"), cats)

    def test_overview_orders_by_normalized_time_and_keeps_original_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            # 字符串降序会把同日凌晨的 ISO（'T' > 空格）排到晚上前面；归一时间
            # 排序必须把 22:00 放在 02:00 之前，两侧都带 +08:00 与机器时区无关。
            (root / "chat2api" / "chat2api.log").write_bytes(
                b"2026-09-21 22:00:00+08:00 INFO evening\n")
            self._share_service(root).write_bytes(
                b"2026-09-21T02:00:00+08:00 INFO morning\n")
            # 该文件首行没有时间：它自成一行（无前驱可挂），ts 保持 None。
            (root / "chat2api" / "chat2api.console.log").write_bytes(
                b"no timestamp at all\n")
            with _overview_env(root):
                result = console_log_reader.read_overview(
                    ledger_paths={"chat2api": str(root / "ledger.jsonl")})
            rows = result["rows"]
            ordered = [row for row in rows
                       if row["ts"] in ("2026-09-21 22:00:00+08:00",
                                        "2026-09-21T02:00:00+08:00")]
            self.assertEqual([row["ts"] for row in ordered],
                             ["2026-09-21 22:00:00+08:00",
                              "2026-09-21T02:00:00+08:00"])
            self.assertEqual([row["ts_norm"] for row in ordered],
                             [1789999200.0, 1789927200.0])
            # 原始文本保留；未知时间的行 ts 保留 None，排在已知时间之后。
            self.assertEqual(ordered[0]["message"], "evening")
            self.assertEqual(ordered[1]["message"], "morning")
            unknown = [row for row in rows if row["ts"] is None]
            self.assertEqual([row["message"] for row in unknown],
                             ["no timestamp at all"])
            self.assertIsNone(unknown[0]["ts_norm"])
            self.assertEqual(rows[-1]["ts"], None)

    def test_overview_failure_is_not_counted_as_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            (root / "chat2api" / "chat2api.log").write_bytes(
                b"2026-09-21 22:00:00 INFO main\n")
            # share 的 service 路径已解析但文件不存在：读取失败/缺席必须如实上报。
            with _overview_env(root):
                result = console_log_reader.read_overview(
                    ledger_paths={"chat2api": str(root / "ledger.jsonl")})
            targets = {(t["source"], t["category"]): t
                       for t in result["targets"]}
            missing = targets[("share", "service")]
            self.assertEqual(missing["status"], "missing")
            self.assertIsNone(missing["rows"])
            self.assertIsNone(missing["next_cursor"])
            coverage = result["coverage"]
            self.assertGreaterEqual(coverage["targets_unread"], 1)
            self.assertGreaterEqual(coverage["targets_missing"], 1)
            # 读取失败的目标行数不计为 0：只有真正读到的行进入 rows_read。
            self.assertEqual(
                coverage["rows_read"],
                sum(t["rows"] for t in result["targets"]
                    if isinstance(t["rows"], int)))
            self.assertFalse(coverage["full_history"])
            self.assertIn("不是完整历史", coverage["note"])

    def test_overview_cursors_are_independent_per_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            data = b"".join(_ts_line(i, width=600) for i in range(450))
            (root / "chat2api" / "chat2api.log").write_bytes(data)
            self._share_service(root).write_bytes(
                b"2026-09-21 22:00:00 INFO share\n")
            with _overview_env(root):
                first = console_log_reader.read_overview(
                    ledger_paths={"chat2api": str(root / "ledger.jsonl")})
                cursors = first["next_cursors"]
                chat_cursor = cursors["chat2api|service"]
                self.assertIsNotNone(chat_cursor)
                # 只回传 chat2api/service 的游标：share 目标仍读尾窗，
                # 不被另一个目标的翻页影响。
                second = console_log_reader.read_overview(
                    cursors=[chat_cursor],
                    ledger_paths={"chat2api": str(root / "ledger.jsonl")})
            targets = {(t["source"], t["category"]): t
                       for t in second["targets"]}
            self.assertFalse(targets[("share", "service")]["reset_hint"])
            self.assertEqual(targets[("share", "service")]["rows"], 1)
            self.assertTrue(
                any(row["source"] == "share" for row in second["rows"]))
            chat_rows = [row for row in second["rows"]
                         if row["source"] == "chat2api"
                         and row["category"] == "service"]
            # 该目标的上游页覆盖 line-0000..0049（50 行，无重叠无空洞），
            # 归一时间降序后 line-0049 在前；不再被行预算截断。
            self.assertEqual(len(chat_rows), 50)
            self.assertIn("line-0049", chat_rows[0]["message"])
            self.assertIn("line-0000", chat_rows[-1]["message"])
            self.assertIsNone(second["next_cursors"]["chat2api|service"])

    def test_overview_marks_truncation_and_unconfigured_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            data = b"".join(_ts_line(i, width=600) for i in range(450))
            (root / "chat2api" / "chat2api.log").write_bytes(data)
            with _overview_env(root):
                result = console_log_reader.read_overview({})
            targets = {(t["source"], t["category"]): t
                       for t in result["targets"]}
            truncated = targets[("chat2api", "service")]
            self.assertTrue(truncated["truncated"])
            self.assertIsNotNone(truncated["next_cursor"])
            # ledger 未注入路径 -> unconfigured（不是「0 行」）。
            self.assertEqual(targets[("chat2api", "ledger")]["status"],
                             "unconfigured")
            self.assertIsNone(targets[("chat2api", "ledger")]["rows"])
            self.assertGreaterEqual(result["coverage"]["targets_truncated"], 1)
            self.assertGreaterEqual(
                result["coverage"]["targets_unconfigured"], 1)

    def test_overview_row_budget_is_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            data = b"".join(_ts_line(i, width=200) for i in range(300))
            (root / "chat2api" / "chat2api.log").write_bytes(data)
            with _overview_env(root):
                result = console_log_reader.read_overview(
                    {"chat2api": str(root / "ledger.jsonl")}, row_budget=50)
            coverage = result["coverage"]
            self.assertEqual(len(result["rows"]), 50)
            self.assertEqual(coverage["rows_returned"], 50)
            self.assertEqual(coverage["rows_read"], 300)
            self.assertEqual(coverage["rows_dropped_by_budget"], 250)
            self.assertEqual(coverage["rows_read"],
                             coverage["rows_returned"]
                             + coverage["rows_dropped_by_budget"])
            # 被合并行预算截断的行如实计进该目标，不是「0 行」。
            targets = {(t["source"], t["category"]): t
                       for t in result["targets"]}
            chat = targets[("chat2api", "service")]
            self.assertEqual(chat["rows"], 300)
            self.assertEqual(chat["rows_returned"], 50)
            self.assertEqual(chat["rows_dropped"], 250)
            # 归一时间降序：留下的 50 行是最新的 line-0250..0299。
            self.assertIn("line-0299", result["rows"][0]["message"])
            self.assertIn("line-0250", result["rows"][-1]["message"])

    def test_overview_reports_source_coverage_without_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            with _overview_env(root):
                result = console_log_reader.read_overview({})
            coverage = result["coverage"]
            self.assertEqual(coverage["scope"], "bounded_tail_window")
            self.assertEqual(coverage["sources_total"],
                             len(result["sources"]))
            console = next(row for row in coverage["sources"]
                           if row["source"] == "console")
            self.assertEqual(console["targets_total"], 0)
            self.assertEqual(console["status"], "no_targets")
            self.assertTrue(console["note"])
