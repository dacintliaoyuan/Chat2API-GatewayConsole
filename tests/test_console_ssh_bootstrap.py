"""SSH-to-browser authorization, without production credentials or services."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest import mock

DISPLAY = Path(__file__).resolve().parents[1] / "um-cloud-deploy" / "display"
CODE = "fixture_bootstrap_code_1234567890"


class ConsoleBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "bootstrap_issuer", DISPLAY / "issue-display-grant.py")
        cls.issuer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.issuer)

    def invoke(self, payload, *, status=200):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.issuer, "read_control_token", return_value="fixture_admin"), \
                mock.patch.object(self.issuer, "request_console_session", create=True,
                                  return_value=(status, json.dumps(payload).encode())), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.issuer.main(["--console-session"])
        return rc, out.getvalue(), err.getvalue()

    def test_console_mode_outputs_only_relative_single_use_bootstrap(self):
        rc, out, err = self.invoke({"ok": True, "code": CODE})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "/display/grant#console=" + CODE + "\n")
        self.assertEqual(err, "")

    def test_rejected_or_malformed_response_does_not_echo_secrets(self):
        for status, payload in [(403, {"code": CODE}), (200, {"ok": False, "code": CODE}),
                                (200, []),
                                (200, {"ok": True, "code": "bad\n" + CODE}),
                                (200, {"ok": True, "code": 123})]:
            with self.subTest(status=status, payload=payload):
                rc, out, err = self.invoke(payload, status=status)
                self.assertNotEqual(rc, 0)
                self.assertEqual(out, "")
                self.assertNotIn(CODE, err)
                self.assertNotIn("fixture_admin", err)

    def test_console_request_uses_existing_admin_endpoint_and_loopback_origin(self):
        response = mock.Mock(status=200)
        response.read.return_value = b'{}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(self.issuer.http.client, "HTTPConnection", return_value=connection):
            self.issuer.request_console_session("http://127.0.0.1:8002", "fixture_admin", 5)
        args, kwargs = connection.request.call_args
        self.assertEqual(args[:2], ("POST", "/api/session/reissue"))
        self.assertEqual(kwargs["headers"]["X-Admin-Control"], "fixture_admin")
        self.assertEqual(kwargs["headers"]["Origin"], "http://127.0.0.1:8002")
        connection.close.assert_called_once()

    def test_console_request_keeps_the_legacy_port_explicit(self):
        # The Rust console is 8002; the Python rollback unit keeps 8096, and the
        # legacy 18096 stays an explicit loopback value for one release cycle.
        response = mock.Mock(status=200)
        response.read.return_value = b'{}'
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(self.issuer.http.client, "HTTPConnection", return_value=connection):
            self.issuer.request_console_session(
                "http://127.0.0.1:18096", "fixture_admin", 5)
        _args, kwargs = connection.request.call_args
        self.assertEqual(kwargs["headers"]["Origin"], "http://127.0.0.1:18096")
        connection.close.assert_called_once()

    def test_console_request_refuses_other_destinations_before_sending_credential(self):
        for origin in ["https://example.org", "http://127.0.0.1:8001",
                       "http://192.168.1.5:8096",
                       "http://127.0.0.1:18096@evil.test", "http://127.0.0.1:18096/?x=1"]:
            with self.subTest(origin=origin), \
                    mock.patch.object(self.issuer.http.client, "HTTPConnection") as connection:
                with self.assertRaises(ValueError):
                    self.issuer.request_console_session(origin, "fixture_admin", 5)
                connection.assert_not_called()


class BrowserBootstrapTests(unittest.TestCase):
    def run_browser(self, fragment, *, ok=True, throws=False):
        # Execute the shipped JS, replacing only the browser and HTTP boundaries.
        harness = r'''
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8')
  .replace("import RFB from './core/rfb.js';", '')
  .replace(/^start\(\);\s*$/m, 'globalThis.done = start();');
const input = JSON.parse(process.argv[2]);
const events = []; const status = {dataset: {}, textContent: ''};
const context = {
  document: {getElementById: id => id === 'status' ? status : {}},
  location: {hash: input.fragment, pathname: '/display/grant', search: '',
             protocol: 'http:', host: '127.0.0.1:18096',
             replace: url => events.push(['redirect', url])},
  history: {replaceState: (...args) => events.push(['clear', args[2]])},
  fetch: async (url, opts) => {
    events.push(['fetch', url, opts]);
    if (input.throws) throw new Error('fixture transport error');
    return {ok: input.ok, json: async () => ({authorized: false})};
  },
  RFB: class {constructor() {events.push(['rfb']);}},
};
vm.createContext(context); vm.runInContext(source, context);
Promise.resolve(context.done).then(() => {
  console.log(JSON.stringify({events, status: status.textContent, tone: status.dataset.state}));
}).catch(e => { console.error('unhandled bootstrap failure'); process.exitCode = 2; });
'''
        result = subprocess.run(
            ["node", "-e", harness, str(DISPLAY / "novnc-launcher.js"),
             json.dumps({"fragment": fragment, "ok": ok, "throws": throws})],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_code_removed_then_exchanged_and_redirected_without_vnc(self):
        result = self.run_browser("#console=" + CODE)
        events = result["events"]
        self.assertEqual(events[0], ["clear", "/display/grant"])
        self.assertEqual(events[1][0:2], ["fetch", "/api/session"])
        self.assertEqual(json.loads(events[1][2]["body"]), {"code": CODE})
        self.assertEqual(events[1][2]["credentials"], "same-origin")
        self.assertEqual(events[-1], ["redirect", "/#overview"])
        self.assertFalse(any(e[0] == "rfb" for e in events))

    def test_expired_code_or_network_error_does_not_redirect_or_connect(self):
        for kwargs in [{"ok": False}, {"throws": True}]:
            with self.subTest(kwargs=kwargs):
                result = self.run_browser("#console=" + CODE, **kwargs)
                self.assertEqual(result["tone"], "error")
                self.assertFalse(any(e[0] in ["redirect", "rfb"] for e in result["events"]))
                self.assertNotIn(CODE, result["status"])

    def test_malformed_console_fragment_is_erased_and_refused(self):
        result = self.run_browser("#console=" + CODE + "&redirect=https://evil.test")
        self.assertEqual(result["events"], [["clear", "/display/grant"]])
        self.assertEqual(result["tone"], "error")

    def test_existing_display_grant_still_uses_display_exchange(self):
        result = self.run_browser("#grant=" + CODE)
        self.assertEqual(result["events"][0][0], "clear")
        self.assertEqual(result["events"][1][1], "/display/grant")
        self.assertEqual(result["events"][2][1], "/display/session")


class EntryHelperPortContractTests(unittest.TestCase):
    """The PC entry and the cloud helper must agree on the console port.

    #97: the entry forwarded 8002 while the deployed helper still whitelisted
    8096/18096, so every shortcut open failed before the bootstrap request was
    sent.  The helper's own port tests cannot see that mismatch: the entry
    default lives in ``cloud/Open-CloudConsole.ps1``.
    """

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "bootstrap_issuer", DISPLAY / "issue-display-grant.py")
        cls.issuer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.issuer)
        entry = (Path(__file__).resolve().parents[1]
                 / "cloud" / "Open-CloudConsole.ps1")
        cls.entry_text = entry.read_text(encoding="utf-8-sig")

    def test_entry_default_console_port_is_accepted_by_the_helper(self):
        match = re.search(
            r"\[int\]\$CloudConsolePort\s*=\s*(\d+)", self.entry_text)
        self.assertIsNotNone(
            match, "the entry must declare a default console port")
        default_port = int(match.group(1))
        self.assertIn(default_port, self.issuer.CONSOLE_PORTS)
        self.assertNotEqual(default_port, 8001)


if __name__ == "__main__":
    unittest.main()
