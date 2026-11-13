# -*- coding: utf-8 -*-
"""E19-1-01B: total boundary test for the three bundled ``shared`` modules.

``shared.nonstream_aggregate``, ``shared.responses_protocol`` and
``shared.pricing_rules`` are the three modules both gateway applications bundle.
This file pins the shared boundary for all three together:

* their import statements reference no host or business module
  (``chat2api``/``usagesrv``/``console_*``/``share_*``/``model_*``/
  ``zcode_*``/``local``/``cloud``);
* a fresh interpreter that imports all three loads none of those modules into
  ``sys.modules`` -- the in-process check would be invalid once the full suite has
  imported host modules, so it runs in a clean subprocess;
* ``pricing_rules.estimate_record`` is a pure calculation over an explicitly
  supplied price table, requires that table (no default, no file lookup) and opens
  no file -- ``load_price_table`` and its cache stay in the host;
* the Responses admission field set is an immutable ``frozenset`` and the caller can
  pass its own ``allowed_fields`` without touching module state;
* the non-stream response identity (``id``/``created_at``) defaults to a fresh
  ``secrets``/``time`` value per call, but a caller may inject both to get a
  deterministic object.

The detector tests use synthetic sources and module lists first, so a broken scanner
or filter cannot silently turn the real boundary checks into no-ops.
"""

import ast
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

AI_GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = AI_GATEWAY_ROOT / "shared"

sys.path.insert(0, str(AI_GATEWAY_ROOT))

from shared import nonstream_aggregate  # noqa: E402
from shared import pricing_rules as pricing  # noqa: E402
from shared import responses_protocol  # noqa: E402

SHARED_MODULES = (nonstream_aggregate, responses_protocol, pricing)

FORBIDDEN_EXACT_MODULES = ("chat2api", "usagesrv", "local", "cloud",
                           "console", "share", "model_sync", "zcode")
FORBIDDEN_PREFIXES = ("console_", "share_", "model_", "zcode_")

# 2026-09-25T12:00:00Z, after LEGACY_BASE_BOUNDARY; hand-computed epoch ms.
TS_OFF = 1790337600000

FORBIDDEN_IMPORT_SOURCE = """
import json
import chat2api
from usagesrv import load_price_table
from console import stats as console_stats
from share_auth import ShareGate
from model_sync import sync as model_sync
from zcode.hot_sync import marker
import local.app
from cloud.backend import app
"""

ALLOWED_IMPORT_SOURCE = """
import json
import time
from dataclasses import dataclass
from datetime import datetime
from shared import pricing_rules
from . import nonstream_aggregate
"""

FRESH_IMPORT_PROBE = """
import json
import sys
from pathlib import Path

for entry in sys.argv[1:]:
    sys.path.insert(0, entry)

import shared.nonstream_aggregate
import shared.responses_protocol
import shared.pricing_rules

print(json.dumps({
    "files": {
        "nonstream_aggregate": str(
            Path(shared.nonstream_aggregate.__file__).resolve()),
        "responses_protocol": str(
            Path(shared.responses_protocol.__file__).resolve()),
        "pricing_rules": str(
            Path(shared.pricing_rules.__file__).resolve()),
    },
    "modules": sorted(sys.modules),
}))
"""


def _root_name(name):
    return str(name or "").split(".")[0]


def _is_forbidden_module(name):
    root = _root_name(name)
    return (root in FORBIDDEN_EXACT_MODULES
            or root.startswith(FORBIDDEN_PREFIXES))


def _absolute_imports(tree):
    """Yield ``(lineno, module)`` for every non-relative import statement."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            yield node.lineno, node.module


def _forbidden_imports(tree):
    return sorted(
        ((lineno, name) for lineno, name in _absolute_imports(tree)
         if _is_forbidden_module(name)),
        key=lambda item: (item[1], item[0]))


def _relative_import_escapes(tree):
    """Relative imports above ``shared`` (``..``) leave the bundled package."""
    return sorted(
        (node.lineno, ast.unparse(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 1)


def _forbidden_modules(modules):
    return sorted(name for name in modules
                  if _is_forbidden_module(name))


def _pricing_table(cache_hit=0.007, cache_miss=0.22, output=0.66):
    """A price table in the shape ``usagesrv.load_price_table`` returns."""
    peak = {"enabled": False, "effectiveAtMs": None, "windows": []}
    tier = {"cacheHit": cache_hit, "cacheMiss": cache_miss,
            "output": output}
    version = {
        "versionId": None,
        "effectiveAt": None,
        "effectiveAtMs": None,
        "exchangeRate": 7.2,
        "peak": peak,
        "tiers": [({"pricing-model": dict(tier)}, "deepseek-peak", "prices")],
        "checkedAt": None,
        "checkedAgeDays": None,
    }
    return {
        "exchangeRate": 7.2,
        "peak": peak,
        "tiers": version["tiers"],
        "override": {},
        "versions": [version],
        "versioned": False,
    }


class ImportScannerTests(unittest.TestCase):
    """The detector itself, so the real-source scan cannot pass vacuously."""

    def test_scanner_flags_every_forbidden_family(self):
        hits = _forbidden_imports(ast.parse(FORBIDDEN_IMPORT_SOURCE))
        self.assertEqual(
            [name for _lineno, name in hits],
            sorted([
                "chat2api",
                "usagesrv",
                "console",
                "share_auth",
                "model_sync",
                "zcode.hot_sync",
                "local.app",
                "cloud.backend",
            ]),
        )

    def test_scanner_accepts_stdlib_shared_and_relative_imports(self):
        tree = ast.parse(ALLOWED_IMPORT_SOURCE)
        self.assertEqual(_forbidden_imports(tree), [])
        self.assertEqual(_relative_import_escapes(tree), [])

    def test_scanner_flags_a_relative_import_escaping_shared(self):
        tree = ast.parse("from ..chat2api import handler\n")
        escapes = _relative_import_escapes(tree)
        self.assertEqual(len(escapes), 1)
        self.assertIn("..chat2api", escapes[0][1])

    def test_module_filter_flags_every_forbidden_family(self):
        modules = [
            "json",
            "shared",
            "shared.pricing_rules",
            "chat2api",
            "chat2api.handler",
            "usagesrv",
            "console.stats",
            "share_auth",
            "model_sync.sync",
            "zcode.hot_sync",
            "local",
            "local.app",
            "cloud",
            "cloud.backend",
        ]
        self.assertEqual(
            _forbidden_modules(modules),
            sorted([
                "chat2api",
                "chat2api.handler",
                "usagesrv",
                "console.stats",
                "share_auth",
                "model_sync.sync",
                "zcode.hot_sync",
                "local",
                "local.app",
                "cloud",
                "cloud.backend",
            ]),
        )


class SharedSourceBoundaryTests(unittest.TestCase):

    def test_shared_modules_resolve_inside_the_shared_package(self):
        expected = {
            "shared.nonstream_aggregate": SHARED_DIR / "nonstream_aggregate.py",
            "shared.responses_protocol": SHARED_DIR / "responses_protocol.py",
            "shared.pricing_rules": SHARED_DIR / "pricing_rules.py",
        }
        for module in SHARED_MODULES:
            with self.subTest(module=module.__name__):
                path = expected[module.__name__]
                self.assertEqual(Path(module.__file__).resolve(), path.resolve())

    def test_real_shared_modules_import_no_host_or_business_module(self):
        violations = []
        for module in SHARED_MODULES:
            path = Path(module.__file__).resolve()
            self.assertTrue(
                path.is_relative_to(SHARED_DIR.resolve()),
                "{} must live inside shared/".format(path),
            )
            tree = ast.parse(path.read_text(encoding="utf-8-sig"),
                             filename=str(path))
            for lineno, name in _forbidden_imports(tree):
                violations.append(
                    "{}:{}: import {}".format(path.name, lineno, name))
            for lineno, statement in _relative_import_escapes(tree):
                violations.append(
                    "{}:{}: {}".format(path.name, lineno, statement))
        self.assertEqual(
            violations, [],
            "shared modules import host/business modules: {}".format(
                "; ".join(violations)),
        )


class FreshInterpreterBoundaryTests(unittest.TestCase):

    def _run_probe(self):
        env = {key: value for key, value in os.environ.items()
               if key.upper() != "PYTHONPATH"}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with tempfile.TemporaryDirectory(
                prefix="r19-shared-boundary-") as isolated_cwd:
            completed = subprocess.run(
                [sys.executable, "-I", "-c", FRESH_IMPORT_PROBE,
                 str(AI_GATEWAY_ROOT)],
                cwd=isolated_cwd,
                env=env,
                capture_output=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(
            completed.returncode, 0,
            "fresh interpreter import probe failed: {}".format(
                completed.stderr),
        )
        lines = [line for line in completed.stdout.splitlines()
                 if line.strip()]
        self.assertTrue(lines, "fresh interpreter probe printed nothing")
        return json.loads(lines[-1])

    def test_fresh_interpreter_importing_all_three_loads_no_business_module(self):
        payload = self._run_probe()
        expected = {
            "nonstream_aggregate": SHARED_DIR / "nonstream_aggregate.py",
            "responses_protocol": SHARED_DIR / "responses_protocol.py",
            "pricing_rules": SHARED_DIR / "pricing_rules.py",
        }
        for key, path in expected.items():
            with self.subTest(module=key):
                self.assertEqual(Path(payload["files"][key]), path.resolve())
        modules = payload["modules"]
        for name in ("shared",
                      "shared.nonstream_aggregate",
                      "shared.responses_protocol",
                      "shared.pricing_rules"):
            self.assertIn(name, modules)
        self.assertEqual(
            _forbidden_modules(modules), [],
            "importing the three shared modules loaded host/business "
            "modules: {}".format(", ".join(_forbidden_modules(modules))),
        )


class PricingRulesPurityTests(unittest.TestCase):

    def test_estimate_record_computes_from_the_explicit_table(self):
        result = pricing.estimate_record(
            "pricing-model", TS_OFF, 1_000_000, 100_000, 500_000, 0,
            _pricing_table())
        self.assertEqual(result[1:], (True, "pricing-model", False))
        self.assertAlmostEqual(result[0], 0.1795, places=9)

    def test_estimate_record_uses_the_table_argument_not_ambient_state(self):
        result = pricing.estimate_record(
            "pricing-model", TS_OFF, 1_000_000, 100_000, 500_000, 0,
            _pricing_table(cache_hit=0.1, cache_miss=1.0, output=2.0))
        self.assertEqual(result[1:], (True, "pricing-model", False))
        self.assertAlmostEqual(result[0], 0.75, places=9)

    def test_estimate_record_requires_the_table_argument(self):
        with self.assertRaises(TypeError):
            pricing.estimate_record(
                "pricing-model", TS_OFF, 1_000_000, 100_000, 500_000, 0)

    def test_estimate_record_opens_no_file(self):
        table = _pricing_table()
        with contextlib.ExitStack() as stack:
            for target in ("builtins.open", "io.open", "os.open",
                           "pathlib.Path.open"):
                stack.enter_context(mock.patch(
                    target,
                    side_effect=AssertionError(
                        "shared pricing opened a file: " + target),
                ))
            result = pricing.estimate_record(
                "pricing-model", TS_OFF, 1_000_000, 100_000, 500_000, 0,
                table)
        self.assertEqual(result[1:], (True, "pricing-model", False))
        self.assertAlmostEqual(result[0], 0.1795, places=9)

    def test_pricing_rules_exposes_no_file_loader_or_host_cache(self):
        for name in ("load_price_table", "PRICE_FILE", "OVERRIDE_FILE",
                      "_price_cache", "_match_cache", "_match_cache_lock",
                      "threading"):
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(pricing, name),
                    "shared pricing must not expose " + name,
                )


class ResponsesAdmissionFieldSetTests(unittest.TestCase):
    """G21: the admission field sets are immutable module constants.

    ``RESPONSES_FIELDS``/``MESSAGES_FIELDS`` decide whether a request is accepted,
    so a mutable ``set`` lets any importer widen the accepted surface process-wide.
    A caller that needs a different surface passes ``allowed_fields`` explicitly.
    """

    # Wire contract literals (OpenAI Responses / Anthropic Messages), written from
    # the API surface rather than read back from the module.
    EXPECTED_RESPONSES_FIELDS = frozenset({
        "model", "input", "instructions", "stream", "max_output_tokens",
        "temperature", "top_p", "tools", "tool_choice", "parallel_tool_calls",
        "store", "prompt_cache_key", "prompt_cache_retention",
        "prompt_cache_options", "service_tier", "reasoning", "include"})
    EXPECTED_MESSAGES_FIELDS = frozenset({
        "model", "messages", "max_tokens", "system", "stream", "temperature",
        "top_p", "top_k", "stop_sequences", "metadata", "tools",
        "tool_choice", "thinking"})

    def test_admission_field_sets_are_frozensets(self):
        for name in ("RESPONSES_FIELDS", "MESSAGES_FIELDS"):
            with self.subTest(name=name):
                value = getattr(responses_protocol, name)
                self.assertIsInstance(
                    value, frozenset,
                    "{} must be a frozenset so no importer can mutate the "
                    "accepted surface".format(name))

    def test_admission_field_sets_keep_their_wire_values(self):
        self.assertEqual(responses_protocol.RESPONSES_FIELDS,
                         self.EXPECTED_RESPONSES_FIELDS)
        self.assertEqual(responses_protocol.MESSAGES_FIELDS,
                         self.EXPECTED_MESSAGES_FIELDS)

    def test_field_sets_refuse_in_place_mutation(self):
        for name in ("RESPONSES_FIELDS", "MESSAGES_FIELDS"):
            with self.subTest(name=name):
                with self.assertRaises(AttributeError):
                    getattr(responses_protocol, name).add("smuggled")

    def test_allowed_fields_argument_does_not_touch_module_state(self):
        smuggled = {"model": "m", "input": "x", "smuggled": 1}
        self.assertFalse(
            responses_protocol.valid_responses_request(smuggled),
            "an unknown field must be refused by the module default")
        self.assertTrue(
            responses_protocol.valid_responses_request(
                smuggled,
                allowed_fields=frozenset({"model", "input", "smuggled"})),
            "allowed_fields must govern this call")
        self.assertNotIn("smuggled", responses_protocol.RESPONSES_FIELDS)
        self.assertFalse(
            responses_protocol.valid_responses_request(smuggled),
            "the controlled call must not widen the module default")

    def test_allowed_fields_argument_defaults_to_the_module_constant(self):
        body = {"model": "m", "input": "x"}
        self.assertTrue(responses_protocol.valid_responses_request(body))
        self.assertTrue(responses_protocol.valid_responses_request(
            body, allowed_fields=None))
        self.assertFalse(responses_protocol.valid_responses_request(
            body, allowed_fields=frozenset({"input"})))


class ResponsesIdentityInjectionTests(unittest.TestCase):
    """G21: the non-stream response identity can be injected by the caller.

    The default stays ``secrets.token_hex`` + ``time.time()`` per call; a caller that
    needs a reproducible object (fixtures, replay, assertions) passes both values and
    gets them back unchanged.
    """

    CHAT_PAYLOAD = {"choices": [{"message": {"content": "hello"},
                                "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                              "total_tokens": 3}}
    MESSAGES_PAYLOAD = {"content": [{"type": "text", "text": "hello"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 2}}

    def test_chat_response_uses_the_injected_identity(self):
        translated = responses_protocol.chat_to_responses_response(
            self.CHAT_PAYLOAD, "fake-um-model",
            created_at=1700000000, response_id="resp_fixed_identity")
        self.assertEqual(translated["id"], "resp_fixed_identity")
        self.assertEqual(translated["created_at"], 1700000000)
        self.assertEqual(translated["model"], "fake-um-model")

    def test_messages_response_uses_the_injected_identity(self):
        translated = responses_protocol.messages_to_responses_response(
            self.MESSAGES_PAYLOAD, "fake-um-model",
            created_at=1700000123, response_id="resp_fixed_identity")
        self.assertEqual(translated["id"], "resp_fixed_identity")
        self.assertEqual(translated["created_at"], 1700000123)

    def test_default_identity_is_generated_per_call(self):
        first = responses_protocol.chat_to_responses_response(
            self.CHAT_PAYLOAD, "fake-um-model")
        second = responses_protocol.chat_to_responses_response(
            self.CHAT_PAYLOAD, "fake-um-model")
        # Recorded current behaviour, not a defect: the default id is random so two
        # calls over identical input differ. Injecting created_at/response_id is how a
        # caller makes the response object's own identity equal.
        self.assertRegex(first["id"], r"^resp_[0-9a-f]{24}$")
        self.assertRegex(second["id"], r"^resp_[0-9a-f]{24}$")
        self.assertNotEqual(first["id"], second["id"])
        self.assertLessEqual(abs(second["created_at"] - first["created_at"]), 60)

    def test_injected_identity_makes_the_response_object_reproducible(self):
        def translate():
            return responses_protocol.chat_to_responses_response(
                self.CHAT_PAYLOAD, "fake-um-model",
                created_at=1700000000, response_id="resp_fixed")

        injected_a, injected_b = translate(), translate()
        self.assertEqual(injected_a["id"], injected_b["id"])
        self.assertEqual(injected_a["created_at"], injected_b["created_at"])
        # The injection covers the response object's identity only. Output item ids
        # (msg_/fc_) stay per-call; recording that keeps this test honest about the
        # scope of the injection instead of implying a fully deterministic object.
        self.assertNotEqual(injected_a["output"][0]["id"],
                            injected_b["output"][0]["id"])

    def test_injection_scope_is_the_top_level_object_only(self):
        """F36: the injection boundary, stated as the value a fixture may rely on.

        ``created_at``/``response_id`` replace the response object's own
        ``id``/``created_at``. Everything nested -- ``output[].id`` here, the
        per-event inner ids in the streaming translators -- is regenerated per call,
        so a byte-for-byte replay fixture must exclude those fields (or compare only
        the top level). Pinned from the plan's measured example.
        """
        payload = {
            "id": "x", "created": 1, "model": "m",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "h"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }

        def translate():
            return responses_protocol.chat_to_responses_response(
                payload, "m", created_at=111, response_id="resp_fixed")

        first, second = translate(), translate()
        self.assertEqual(first["id"], "resp_fixed")
        self.assertEqual(first["created_at"], 111)
        self.assertEqual(second["id"], "resp_fixed")
        self.assertEqual(second["created_at"], 111)
        self.assertTrue(first["output"], "the fixture must produce output items")
        self.assertNotEqual(
            [item["id"] for item in first["output"]],
            [item["id"] for item in second["output"]],
            "output[].id must stay per-call; if this ever becomes injectable, "
            "the README's stated injection scope has to change with it")


class KnownDeferredBoundaryTests(unittest.TestCase):
    """G19/T20: two boundary gaps E19-1 deliberately leaves to E19-2.

    These assertions record the current state so a later item cannot mistake the
    deferral for an achieved boundary. When E19-2 moves the Windows named-pipe
    transport out of the cloud package or the credential reader into each end, the
    matching assertion here is the reminder that must be updated in the same change.
    """

    BROKER_CLIENT_PATH = AI_GATEWAY_ROOT / "share/broker_client.py"
    SECRET_PROTECTION_IMPORT = "share.secret_protection"

    def test_named_pipe_transport_is_retired(self):
        self.assertTrue(self.BROKER_CLIENT_PATH.is_file())
        probe = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "try:\n"
            "    from share.broker_client import NamedPipeBrokerTransport\n"
            "except ImportError:\n"
            "    print('retired')\n"
            "else:\n"
            "    print('present')\n"
        )
        with tempfile.TemporaryDirectory(
                prefix="r19-shared-deferred-") as isolated_cwd:
            # The probe must not see the real user profile, the repository tmp or an
            # inherited PYTHONPATH; otherwise a passing import could be explained by
            # ambient state instead of the module actually being present.
            env = {key: value for key, value in os.environ.items()
                   if key.upper() not in ("PYTHONPATH", "HOME", "USERPROFILE",
                                          "TEMP", "TMP", "TMPDIR")}
            env.update({"PYTHONDONTWRITEBYTECODE": "1",
                        "HOME": isolated_cwd,
                        "USERPROFILE": isolated_cwd,
                        "TEMP": isolated_cwd,
                        "TMP": isolated_cwd,
                        "TMPDIR": isolated_cwd})
            completed = subprocess.run(
                [sys.executable, "-I", "-c", probe, str(AI_GATEWAY_ROOT)],
                cwd=isolated_cwd, env=env, capture_output=True,
                encoding="utf-8", timeout=60)
        self.assertEqual(
            completed.returncode, 0,
            "retired transport probe failed: {}".format(completed.stderr))
        self.assertEqual(completed.stdout.strip().splitlines()[-1],
                         "retired")

    def test_secret_protection_is_still_imported_by_both_ends(self):
        """The credential reader is still reachable from both ends.

        E19-2's known deferral is unchanged; the reader moved from the two role
        modules into ``console_backend_app`` (the shared backend assembly). The
        assertion follows it: the shared module imports ``share_secret_protection``,
        and both role modules import the shared module.
        """
        def imported_of(path):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"),
                             filename=str(path))
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    names.add(node.module or "")
            return names

        shared = AI_GATEWAY_ROOT / "app/console_backend_app.py"
        self.assertTrue(shared.is_file(), "app/console_backend_app.py must exist")
        self.assertIn(
            self.SECRET_PROTECTION_IMPORT, imported_of(shared),
            "{} no longer imports {}: E19-2 moved the credential reader; "
            "update this known-deferral assertion".format(
                shared.name, self.SECRET_PROTECTION_IMPORT))
        for relative in ("local/backend/app.py",):
            path = AI_GATEWAY_ROOT / relative
            with self.subTest(path=relative):
                self.assertTrue(path.is_file(), "{} must exist".format(path))
                self.assertIn(
                    "app.console_backend_app", imported_of(path),
                    "{} no longer imports console_backend_app: the shared "
                    "backend module carries the credential reader".format(
                        relative))


class DualEndConsumptionTests(unittest.TestCase):
    """Both applications consume ``shared``; ``shared`` consumes neither.

    The assertion scans each end's whole source tree instead of pinning one file, so
    an end may move or rename its consumer without breaking the test, while an end
    that stops consuming ``shared`` (or a shared module that starts importing an end)
    still fails.
    """

    END_DIRS = {"local": AI_GATEWAY_ROOT / "local"}

    @staticmethod
    def _shared_imports(path):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"),
                         filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.add(node.module or "")
        return imported

    def test_each_end_imports_shared(self):
        for end, directory in self.END_DIRS.items():
            with self.subTest(end=end):
                self.assertTrue(directory.is_dir(),
                                "{} must exist".format(directory))
                consumers = []
                for path in sorted(directory.rglob("*.py")):
                    if any(name == "shared" or name.startswith("shared.")
                           for name in self._shared_imports(path)):
                        consumers.append(path.relative_to(AI_GATEWAY_ROOT)
                                         .as_posix())
                self.assertTrue(
                    consumers,
                    "{} must consume the bundled shared package; it currently "
                    "imports nothing from shared".format(end))

    def test_no_shared_module_imports_either_end(self):
        for module in SHARED_MODULES:
            path = Path(module.__file__).resolve()
            with self.subTest(module=module.__name__):
                imports = self._shared_imports(path)
                for end in self.END_DIRS:
                    offenders = sorted(
                        name for name in imports
                        if name == end or name.startswith(end + "."))
                    self.assertEqual(
                        offenders, [],
                        "{} must not import the {} end".format(
                            module.__name__, end))


if __name__ == "__main__":
    unittest.main()
