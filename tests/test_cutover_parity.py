# -*- coding: utf-8 -*-
"""#72: cutover data parity and the acceptance gate fixture.

``tests/fixtures/cutover_parity/`` is written by the REAL Python owners
(``share.store.ShareStore``, ``share.runtime.ProtectedProjectionWriter``,
``chat2api.record_usage``, ``chat2api.TokenStore`` and the profile lock
writer).  The Rust ``cutover_parity`` test reads that same directory; this
module proves the Python side reads it back and that the committed fixture is
fresh.

Only temporary directories and the committed fixture are read or written; no
production path, real browser or UM endpoint is touched.
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "tests" / "fixtures" / "cutover_parity"
FIXTURE = ROOT / "tests" / "fixtures" / "rust_cutover_parity_rev1.json"

sys.path.insert(0, str(ROOT / "tests"))


def _generator():
    import generate_cutover_parity_fixture as generator

    return generator


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class CutoverParityFixtureTests(unittest.TestCase):
    def test_fixture_is_fresh_from_the_real_python_owners(self):
        generator = _generator()
        self.assertEqual(
            generator.generate(),
            _fixture(),
            "cutover parity fixture is stale; run "
            "python tests/generate_cutover_parity_fixture.py",
        )

    def test_committed_data_files_match_the_fixture(self):
        fixture = _fixture()
        for relative, expected in fixture["dataFiles"].items():
            with self.subTest(path=relative):
                path = DATA_DIR / relative
                self.assertTrue(path.is_file(), "missing fixture data file")
                self.assertEqual(
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    expected["sha256"],
                    "fixture data file is stale",
                )
                self.assertEqual(path.stat().st_size, expected["size"])

    def test_python_reads_the_python_written_store_fixture(self):
        generator = _generator()
        fixture = _fixture()
        with tempfile.TemporaryDirectory(prefix="cutover-store-") as tmp:
            db = Path(tmp) / "share-store.db"
            sql = (DATA_DIR / "share-store.sql").read_text(encoding="utf-8")
            connection = sqlite3.connect(str(db))
            try:
                connection.executescript(sql)
            finally:
                connection.close()
            self.assertEqual(
                generator._store_views(db),
                fixture["python"]["store"],
            )

    def test_python_reads_the_python_written_ledger_fixture(self):
        generator = _generator()
        fixture = _fixture()
        ledger = DATA_DIR / "share-backend" / "usage-share-backend.jsonl"
        self.assertEqual(
            generator._ledger_views(ledger),
            fixture["python"]["ledger"],
        )

    def test_python_reads_the_python_written_projection_fixture(self):
        generator = _generator()
        fixture = _fixture()
        path = DATA_DIR / "run" / "share-projection.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = fixture["python"]["projection"]
        parsed = generator.protocol.parse_projection_payload(payload)
        from share.management import _fingerprint

        fingerprint = _fingerprint(payload)
        self.assertEqual(parsed, expected["parsed"])
        self.assertEqual(fingerprint, expected["fingerprint"])

        with tempfile.TemporaryDirectory(prefix="cutover-projection-") as tmp:
            target = Path(tmp) / "share-projection.json"
            writer = generator.runtime.ProtectedProjectionWriter(
                target, lambda _path: None)
            self.assertEqual(
                writer.write(payload, payload["config_generation"], fingerprint),
                fingerprint,
            )
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")), payload)

    def test_python_reads_the_python_written_token_and_profile_fixture(self):
        generator = _generator()
        fixture = _fixture()
        self.assertEqual(
            generator._token_view(DATA_DIR / "share-backend" / "token.json"),
            fixture["python"]["token"],
        )
        self.assertEqual(
            generator._profile_view(
                DATA_DIR / "share-backend" / "edge-profile"),
            fixture["python"]["profile"],
        )

    def test_python_reads_a_rust_append_row_shape(self):
        generator = _generator()
        row = {
            "time": "2026-11-16T08:00:00+08:00",
            "service": "chat2api",
            "model": "cutover-append-model",
            "stream": False,
            "status": "ok",
            "input_tokens": 21,
            "output_tokens": 3,
            "cached_input_tokens": 1,
            "cache_report_status": "reported",
            "reasoning_tokens": 0,
            "total_tokens": 24,
            "estimated": False,
            "duration_ms": 0,
            "usage_source": "upstream",
            "protocol": "chat",
            "key_id": "key-alpha",
            "request_id": "rust-append-1",
            "attribution": "verified",
        }
        with tempfile.TemporaryDirectory(prefix="cutover-rust-row-") as tmp:
            ledger = Path(tmp) / "usage-share-backend.jsonl"
            ledger.write_text(
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8", newline="\n")
            summary = generator.metering.read_share_usage(
                ledger, now=generator.FIXED_EPOCH)
            requests = generator.metering.read_share_requests(
                ledger, now=generator.FIXED_EPOCH)
            self.assertEqual(summary["requests"], 1)
            self.assertEqual(summary["keys"][0]["key_id"], "key-alpha")
            self.assertEqual(
                summary["keys"][0]["tokenUsage"]["completeRequests"], 1)
            self.assertEqual(requests["rows"][0]["keyId"], "key-alpha")
            self.assertEqual(requests["rows"][0]["usageSource"], "upstream")
            self.assertEqual(requests["rows"][0]["inputTokens"], 21)
            self.assertEqual(requests["rows"][0]["cacheTokens"], 1)

    def test_parity_fixture_declares_only_isolated_paths(self):
        fixture = _fixture()
        self.assertTrue(fixture["metadata"]["fixture"])
        self.assertEqual(fixture["metadata"]["ticket"], 72)
        self.assertEqual(
            fixture["metadata"]["productionPaths"],
            ["/var/lib/um-share", "/var/lib/um-share-console",
             "/etc/um-share"],
        )
        for relative in fixture["dataFiles"]:
            with self.subTest(path=relative):
                self.assertFalse(relative.startswith("/"))
                self.assertNotIn(":", relative)
                self.assertNotIn("..", Path(relative).parts)
        self.assertEqual(fixture["python"]["token"]["token"],
                         "fixture-token-value")
        self.assertEqual(fixture["python"]["profile"]["owner"]["pid"], 4242)


if __name__ == "__main__":
    unittest.main()
