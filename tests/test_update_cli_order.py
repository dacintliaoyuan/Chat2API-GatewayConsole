# -*- coding: utf-8 -*-
"""R17 R7: brokerctl drain options must precede the ``stop`` subcommand.

The same file pins the release-owned update/rollback entrypoints, the
recovery-mode share-key gate and install's effective share-key reuse: a
standard update/rollback must not run the pre-switch ``current`` script, and an
explicit recovery must not require the live share-key file that the sealed backup
is supposed to restore.

These tests are textual and isolated: they read the shipped shell scripts, split
each ``brokerctl`` call the way the shell would, and hand the arguments to the
REAL brokerctl parser.  The switch-failure tests at the end execute the shipped
update/rollback tail with the real restore/compensation helpers and doubles only at
external I/O.  No unit, socket, broker, release or production path is touched,
and no update or rollback is executed.

Run from ``ai-gateway``:

    python -m unittest tests.test_update_cli_order
"""
import contextlib
import importlib.util
import io
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parents[1]      # ai-gateway/
DEPLOY = HERE / "um-cloud-deploy"
BROKERCTL = DEPLOY / "bin" / "um-share-gatewayctl.py"
# subprocess resolves bare "bash" to the WSL launcher on Windows; the shell
# helpers must run under the Git Bash that this checkout's tests use.
BASH = shutil.which("bash") or "bash"
SHELL_SCRIPTS = ("update.sh", "rollback.sh")

# Options declared on the top-level parser (before add_subparsers).
GLOBAL_OPTIONS = frozenset({
    "--socket",
    "--key-file",
    "--timeout",
    "--port",
    "--drain-budget",
    "--broker-drain-max",
    "--poll-interval",
    "--restart-budget",
})
SUBCOMMANDS = frozenset({
    "status",
    "start",
    "start-explicit",
    "drain",
    "stop",
    "restart",
    "gateway-restart",
})
# Only ``stop`` declares its own option; every other option is top-level.
SUBCOMMAND_OPTIONS = frozenset({"--force"})

# ``$DRAIN_BUDGET``/``$DRAIN_MAX`` are the script's own defaults
# (``UM_DRAIN_BUDGET_S=900`` and min(90, budget - 1)); argparse needs a
# concrete integer, and only the option ORDER is under test.
SHELL_DEFAULTS = {"$DRAIN_BUDGET": "900", "$DRAIN_MAX": "90"}

# ``brokerctl [<release>] <args>`` up to the first shell control
# operator/redirection.  ``brokerctl_release`` names the release whose CLI the
# script verified; that leading path is not an argparse argument.
CALL_RE = re.compile(
    r"\bbrokerctl(?:_release\s+(?P<release>\S+))?\s+"
    r"(?P<args>[^;|&]*?)(?=\s+2>|\s*\|\||\s*;|\s*$)")


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def _posix(path):
    """Return the path the Git Bash that runs the harness sees."""
    text = str(Path(path).resolve()).replace("\\", "/")
    if len(text) > 1 and text[1:2] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _logical_lines(text):
    """Join backslash continuations so one shell call stays one line."""
    pending = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if pending:
            stripped = pending + " " + stripped
            pending = ""
        if stripped.endswith("\\"):
            pending = stripped[:-1].rstrip()
            continue
        yield stripped


def _brokerctl_calls(text):
    """Return every ``brokerctl`` argv list in a shell script, in file order."""
    calls = []
    for line in _logical_lines(text):
        match = CALL_RE.search(line)
        if match:
            calls.append(shlex.split(match.group("args"), posix=True))
    return calls


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "um_share_brokerctl_cli_order", BROKERCTL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_parser():
    return _load_module()._build_parser()


class BrokerctlCallOrderTests(unittest.TestCase):
    def _check_call(self, argv):
        """Assert the argparse contract for one ``brokerctl`` argv list."""
        index = next((i for i, token in enumerate(argv)
                      if token in SUBCOMMANDS), -1)
        self.assertGreaterEqual(index, 0, "no subcommand in %s" % (argv,))
        for position, token in enumerate(argv):
            if not token.startswith("-"):
                continue
            if position < index:
                self.assertIn(token, GLOBAL_OPTIONS,
                              "option %r is not a top-level option" % token)
            else:
                self.assertIn(token, SUBCOMMAND_OPTIONS,
                              "option %r is not a %s option"
                              % (token, argv[index]))
        return index

    def test_every_call_puts_global_options_before_the_subcommand(self):
        for name in SHELL_SCRIPTS:
            for argv in _brokerctl_calls(_read(name)):
                with self.subTest(script=name, argv=argv):
                    self._check_call(argv)

    def test_drain_and_stop_calls_use_the_documented_order(self):
        # The update/rollback budget and the 60/1 emergency-rollback fallback;
        # only the two values and the trailing subcommand are pinned, so an
        # additional top-level option stays legal.
        expected = {
            "update.sh": [("$DRAIN_BUDGET", "5"), ("60", "1")],
            "rollback.sh": [("$DRAIN_BUDGET", "5"), ("60", "1")],
        }
        for name in SHELL_SCRIPTS:
            calls = [argv for argv in _brokerctl_calls(_read(name))
                      if "stop" in argv]
            with self.subTest(script=name):
                self.assertEqual(len(expected[name]), len(calls))
                for argv, (budget, poll) in zip(calls, expected[name]):
                    self.assertEqual("stop", argv[-1])
                    self.assertEqual(
                        budget, argv[argv.index("--drain-budget") + 1])
                    self.assertEqual(
                        poll, argv[argv.index("--poll-interval") + 1])

    def test_the_real_parser_accepts_every_extracted_call(self):
        parser = _load_parser()
        for name in SHELL_SCRIPTS:
            for argv in _brokerctl_calls(_read(name)):
                concrete = [SHELL_DEFAULTS.get(token, token) for token in argv]
                with self.subTest(script=name, argv=argv):
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args(
                            ["--socket", "/tmp/cli-order.sock",
                             "--key-file", "/tmp/cli-order.key"] + concrete)

    def test_the_old_option_order_is_what_argparse_rejects(self):
        # Proves the pin is red-capable: the previous ``stop --drain-budget ...``
        # order is an argparse error, so a revert must fail this suite.
        parser = _load_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                parser.parse_args(
                    ["--socket", "/tmp/cli-order.sock",
                     "--key-file", "/tmp/cli-order.key",
                     "stop", "--drain-budget", "60", "--poll-interval", "1"])
        self.assertEqual(2, caught.exception.code)

    def test_small_drain_budget_enters_the_drain_instead_of_exiting_64(self):
        # update.sh/rollback.sh document 1..900 and the emergency rollback
        # path uses 60. The parser default must shrink to fit that total
        # instead of rejecting the documented range after parsing.
        module = _load_module()
        for budget in ("1", "60", "90", "900"):
            with self.subTest(budget=budget):
                calls = []

                class FakeClient:
                    def drain(self):
                        calls.append("drain")
                        return SimpleNamespace(
                            ok=True, error_code=None,
                            result={"drained": True, "stopped": True})

                    def host_stop(self):
                        calls.append("host-stop")
                        return SimpleNamespace(
                            ok=True, error_code=None,
                            result={"host_stopped": True})

                with mock.patch.object(
                        module, "_wait_connections_zero", return_value=True), \
                        mock.patch.object(module, "_client",
                                          return_value=FakeClient()):
                    code = module.main([
                        "--drain-budget", budget,
                        "--poll-interval", "1", "stop"])
                self.assertEqual(0, code)
                self.assertEqual(["drain", "host-stop"], calls)

    def test_explicit_contradictory_broker_drain_max_is_still_refused(self):
        module = _load_module()
        with contextlib.redirect_stderr(io.StringIO()):
            code = module.main([
                "--broker-drain-max", "90", "--drain-budget", "60", "stop"])
        self.assertEqual(64, code)

    def test_the_broker_unit_execstop_uses_the_same_order(self):
        unit = (DEPLOY / "systemd" / "um-share-broker.service").read_text(
            encoding="utf-8")
        exec_stop = next(line for line in unit.splitlines()
                         if line.startswith("ExecStop="))
        # The Rust release renamed the CLI; the Python rollback unit keeps the
        # old file name. Split after the .py token so both names parse.
        argv = shlex.split(exec_stop.split(".py", 1)[1], posix=True)
        self.assertIn("stop", argv)
        self.assertEqual("stop", argv[-1])
        self._check_call(argv)

    def test_the_60_second_emergency_drain_satisfies_the_old_cli_check(self):
        # The old current CLI (r16) has no clamping default: it rejects
        # --drain-budget 60 unless --broker-drain-max is explicit and below
        # the budget.  The emergency path must therefore name it.
        for name in SHELL_SCRIPTS:
            calls = [argv for argv in _brokerctl_calls(_read(name))
                     if "stop" in argv and "60" in argv]
            self.assertEqual(1, len(calls), name)
            for argv in calls:
                self.assertIn("--broker-drain-max", argv)
                budget = int(argv[argv.index("--drain-budget") + 1])
                broker_max = int(argv[argv.index("--broker-drain-max") + 1])
                self.assertGreaterEqual(broker_max, 1)
                self.assertLess(broker_max, budget)
                self.assertLessEqual(broker_max, 90)

    def test_the_normal_drain_names_a_broker_max_the_old_cli_accepts(self):
        # The normal rollback and the update's explicit recovery may run the
        # live current CLI (r16).  That CLI rejects every budget <= its
        # default 90-second reservation, so the call must name
        # min(90, budget - 1) explicitly.
        for name in SHELL_SCRIPTS:
            calls = [argv for argv in _brokerctl_calls(_read(name))
                     if "stop" in argv and "$DRAIN_BUDGET" in argv]
            self.assertEqual(1, len(calls), name)
            argv = calls[0]
            self.assertIn("--broker-drain-max", argv)
            self.assertLess(argv.index("--broker-drain-max"),
                            argv.index("--drain-budget"))
            self.assertEqual("$DRAIN_MAX",
                             argv[argv.index("--broker-drain-max") + 1])

    def test_the_shell_drain_max_fits_every_accepted_budget(self):
        # The old CLI accepts a budget only when the explicit broker
        # reservation is 1..90 and strictly smaller.  The helper is the same
        # code the scripts run for their accepted 2..900 range.
        for budget in ("2", "60", "90", "900"):
            proc = subprocess.run(
                [BASH, "-c",
                 'set -euo pipefail; . "$1"; broker_drain_max_for_budget "$2"',
                 BASH, str(DEPLOY / "lib" / "common.sh"), budget],
                capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)
            broker_max = int(proc.stdout.strip())
            self.assertGreaterEqual(broker_max, 1)
            self.assertLess(broker_max, int(budget))
            self.assertLessEqual(broker_max, 90)

    def test_the_scripts_refuse_a_one_second_budget_clearly(self):
        # A 1-second budget leaves no positive broker reservation.  The old CLI
        # cannot run it, so the scripts refuse it with a clear message instead
        # of letting the old parser exit 64.
        for name in SHELL_SCRIPTS:
            self.assertIn("2..900", _read(name))
        proc = subprocess.run(
            [BASH, "-c",
             'set -euo pipefail; . "$1"; broker_drain_max_for_budget 1',
             BASH, str(DEPLOY / "lib" / "common.sh")],
            capture_output=True, text=True)
        self.assertEqual(64, proc.returncode)
        self.assertIn("at least 2", proc.stderr)

    def test_a_new_share_key_is_refused_when_the_store_has_aesgcm_rows(self):
        # install's idempotent path must not create a new key when an existing
        # store still depends on the missing one: those AES-GCM rows would
        # become permanently undecryptable.
        def posix_path(path):
            if os.name != "nt":
                return str(path)
            proc = subprocess.run(
                ["cygpath", "-u", str(path)],
                capture_output=True, text=True)
            self.assertEqual(0, proc.returncode, proc.stderr)
            return proc.stdout.strip()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            etc = root / "etc"
            console = root / "console"
            binp = root / "bin"
            etc.mkdir()
            console.mkdir()
            binp.mkdir()
            wrapper = binp / "python3.14"
            wrapper.write_text(
                '#!/usr/bin/env bash\nexec "%s" "$@"\n'
                % sys.executable.replace("\\", "/"), encoding="utf-8")
            wrapper.chmod(0o755)
            store = console / "share-store.db"
            (etc / "console.env").write_text(
                f"SHARE_STORE_FILE={store}\n", encoding="utf-8")
            conn = sqlite3.connect(store)
            try:
                conn.execute(
                    "CREATE TABLE keys(secret_blob BLOB, secret_algo TEXT)")
                conn.execute(
                    "INSERT INTO keys VALUES(?,?)",
                    (b"x", "aesgcm-file-key-v1"))
                conn.commit()
            finally:
                conn.close()
            target = console / "share-secret.key"
            env = dict(
                os.environ,
                PATH=posix_path(binp) + ":" + os.environ.get("PATH", ""),
                UM_ETC=posix_path(etc),
                UM_CONSOLE_VAR=posix_path(console),
                UM_BROKER_USER="root", UM_BROKER_GROUP="root",
                UM_CONSOLE_USER="root", UM_CONSOLE_GROUP="root")
            proc = subprocess.run(
                [BASH, "-c",
                 'set -euo pipefail; . "$1"; create_share_secret_file "$2"',
                 BASH, posix_path(DEPLOY / "lib" / "common.sh"),
                 posix_path(target)],
                capture_output=True, text=True, env=env)
            self.assertEqual(66, proc.returncode, proc.stderr)
            self.assertIn("refusing to create a different key", proc.stderr)
            self.assertFalse(target.exists())

    def test_install_recovery_uses_the_persistent_key_path_without_creating_one(self):
        # Explicit recovery restores the key carried by the sealed backup; it
        # must only name the persistent path, never invent a new key at a
        # configured path whose live file is missing.
        install = _read("install.sh")
        self.assertIn(
            'if [ -n "$RECOVER_BACKUP" ]; then\n'
            '  ensure_share_secret_wiring recover\n'
            'else\n'
            '  ensure_share_secret_wiring install\n'
            'fi',
            install.replace("\r\n", "\n"))
        common = _read("lib/common.sh")
        self.assertIn("share_store_has_encrypted_rows", common)
        self.assertIn("refuse_new_share_key_for_existing_rows", common)
        self.assertIn("aesgcm-file-key-v1", common)
        self.assertIn('"$mode" = "recover"', common)
        self.assertIn("refusing to create a different key", common)

    def test_install_recovery_passes_the_console_key_owner(self):
        # The restore runs as root, while the console's key-file gate accepts
        # only a 0600 file owned by its own uid.  Without the owner/group the
        # restored custom-path key stays root:root and the console cannot read it.
        install = _read("install.sh")
        recovery = install[install.index("run_recovery()"):]
        recovery = recovery[:recovery.index("\n}\n")]
        self.assertIn('--share-secret-owner "$(id -u "$UM_CONSOLE_USER")"',
                      recovery)
        self.assertIn('--share-secret-group "$(id -g "$UM_CONSOLE_GROUP")"',
                      recovery)

    def test_install_runs_explicit_recovery_even_with_no_start(self):
        # --no-start skips starting services; it must not skip the restore the
        # operator explicitly asked for.  A failed restore must abort non-zero
        # instead of finishing with a 0 exit and no restored data.
        install = _read("install.sh").replace("\r\n", "\n")
        gate = install.index('if [ "$NO_START" -eq 0 ]; then')
        recovery = install.index('run_recovery "$TARGET"')
        self.assertLess(
            recovery, gate,
            "explicit recovery is still inside the --no-start gate")
        self.assertIn(
            'if [ -n "$RECOVER_BACKUP" ]; then\n'
            '  log "explicit recovery mode: restoring $RECOVER_BACKUP"\n'
            '  run_recovery "$TARGET"\n'
            'fi\n',
            install,
            "explicit recovery is not an unconditional branch of its own")
        self.assertNotIn('run_recovery "$TARGET" ||', install,
                         "a failed recovery must not be swallowed")
        self.assertNotIn('run_recovery "$TARGET" &&', install)
        # The no-start branch still skips every service start and health gate.
        no_start = install[gate:]
        self.assertIn('systemctl restart "$TARGET_GATEWAY_UNIT"', no_start)
        self.assertIn('systemctl restart "$TARGET_CONSOLE_UNIT"', no_start)
        self.assertIn("brokerctl start-explicit", no_start)
        self.assertIn('wait_port_open 127.0.0.1 "$TARGET_CONSOLE_PORT"', no_start)
        self.assertIn('wait_port_open 127.0.0.1 "$HEALTH_PORT"', no_start)
        self.assertIn("services were not started (--no-start)", no_start)

    def test_no_start_still_runs_the_explicit_recovery(self):
        # Run the shipped tail (recovery branch + start gate) with the service
        # commands stubbed out.  --no-start must call the restore and skip every
        # service start; a failed restore must abort non-zero.
        install = _read("install.sh").replace("\r\n", "\n")
        marker = 'check_deployment_assembly "$TARGET"\n'
        tail = install[install.index(marker) + len(marker):]

        def run(no_start, recover, recovery_body=""):
            with tempfile.TemporaryDirectory(prefix="install-tail-") as tmp:
                trace = Path(tmp) / "trace"
                trace_arg = str(trace)
                if os.name == "nt":
                    converted = subprocess.run(
                        ["cygpath", "-u", str(trace)],
                        capture_output=True, text=True)
                    self.assertEqual(0, converted.returncode, converted.stderr)
                    trace_arg = converted.stdout.strip()
                env = dict(
                    os.environ, TRACE=trace_arg, TARGET="/tmp/um-share-test",
                    VERSION="test", UM_PREFIX="/opt/um-share",
                    UM_HEALTH_TIMEOUT_S="1", NO_START=no_start,
                    RECOVER_BACKUP=recover, WITH_DISPLAY="0",
                    WITH_CLOUDFLARED="0", TARGET_RUNTIME="python",
                    HEALTH_PORT="8100",
                    EX_OK="0", EX_SOFTWARE="70", EX_UNAVAILABLE="69")
                recovery_tail = f"{recovery_body};" if recovery_body else ""
                script = f"""
set -euo pipefail
log() {{ printf 'LOG:%s\\n' "$*" >&2; }}
die() {{ printf 'DIE:%s\\n' "$2" >&2; exit "$1"; }}
systemctl() {{ printf 'systemctl %s\\n' "$*" >> "$TRACE"; }}
brokerctl() {{ printf 'brokerctl %s\\n' "$*" >> "$TRACE"; }}
console_port() {{ printf '8096\\n'; }}
release_console_unit() {{ case "$1" in rust) printf 'um-share-console.service\\n' ;; *) printf 'um-share-console-python.service\\n' ;; esac; }}
release_console_port() {{ case "$1" in rust) printf '8002\\n' ;; *) printf '8096\\n' ;; esac; }}
wait_port_open() {{ printf 'wait_port_open %s\\n' "$*" >> "$TRACE"; }}
health_gate() {{ printf 'health_gate\\n' >> "$TRACE"; }}
run_recovery() {{ printf 'recovery %s\\n' "$1" >> "$TRACE"; {recovery_tail} }}
{tail}
"""
                proc = subprocess.run([BASH, "-c", script], capture_output=True,
                                      text=True, env=env)
                return proc, trace.read_text(encoding="utf-8") \
                    if trace.exists() else ""

        proc, trace = run("1", "/tmp/backup.umbak")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("recovery /tmp/um-share-test", trace,
                      "--no-start skipped the explicit recovery")
        self.assertNotIn("systemctl restart", trace,
                         "--no-start started a service")
        self.assertNotIn("wait_port_open", trace)
        self.assertNotIn("checkpoint", trace)

        proc, trace = run("1", "")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("recovery", trace)
        self.assertNotIn("systemctl restart", trace)

        proc, trace = run("1", "/tmp/backup.umbak",
                          'die "explicit data recovery failed" "$EX_SOFTWARE"')
        self.assertNotEqual(0, proc.returncode,
                            "a failed recovery finished with a 0 exit")
        self.assertIn("recovery /tmp/um-share-test", trace)
        self.assertNotIn("systemctl restart", trace)

        proc, trace = run("0", "/tmp/backup.umbak")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("recovery /tmp/um-share-test", trace)
        self.assertIn("systemctl restart um-share-broker.service", trace)
        self.assertIn("brokerctl start-explicit", trace)
        self.assertNotIn("checkpoint", trace,
                         "explicit recovery must skip the post-install checkpoint")

    def test_the_drain_uses_an_installed_release_cli(self):
        # A normal update drains with the verified target it just installed;
        # explicit recovery keeps the live current release (no switch).  Rollback
        # drains with the live current release, which is installed and readable
        # by the broker user.  None of them relies on the old "current" bin
        # entry or on an unpacked archive outside /opt/um-share.
        update = _read("update.sh")
        self.assertIn('DRAIN_RELEASE="$TARGET"', update)
        self.assertIn('DRAIN_RELEASE="$CURRENT_TARGET"', update)
        self.assertNotIn('DRAIN_RELEASE="$RELEASE_ROOT"', update)
        self.assertIn('brokerctl_release "$DRAIN_RELEASE"', update)
        rollback = _read("rollback.sh")
        self.assertIn('brokerctl_release "$CURRENT_TARGET"', rollback)
        self.assertNotIn('brokerctl_release "$RELEASE_ROOT"', rollback)

    def test_readme_runs_the_release_owned_update_and_rollback(self):
        # The documented standard entrypoint must be the new release's script.
        # /opt/um-share/current still points at the old release before the
        # first switch, so running it would keep the old drain order and
        # recovery code.
        commands = []
        pending = ""
        in_block = False
        for line in _read("README.md").splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_block = not in_block
                pending = ""
                continue
            if not in_block:
                continue
            if pending:
                stripped = pending + " " + stripped
                pending = ""
            if stripped.startswith("#"):
                continue
            if stripped.endswith("\\"):
                pending = stripped[:-1].rstrip()
                continue
            if re.search(r"um-cloud-deploy/(?:update|rollback)\.sh",
                          stripped):
                commands.append(stripped)
        self.assertTrue(commands)
        for command in commands:
            self.assertNotRegex(
                command,
                r"/opt/um-share/current/um-cloud-deploy/"
                r"(?:update|rollback)\.sh")
        self.assertTrue(any(
            "/.incoming/um-share-<version>/um-cloud-deploy/update.sh"
            in command for command in commands))
        self.assertTrue(any(
            "/releases/<current-version>/um-cloud-deploy/rollback.sh"
            in command for command in commands))
        recovery = [command for command in commands
                    if "--recover-backup" in command]
        self.assertTrue(recovery)
        for command in recovery:
            self.assertIn(
                "/releases/<current-version>/um-cloud-deploy/rollback.sh",
                command)

    def test_explicit_recovery_does_not_gate_on_the_live_share_key(self):
        # A configured live key may be missing precisely because the sealed
        # backup must restore it.  The strict wiring may only run outside the
        # explicit-recovery branch.
        for name in SHELL_SCRIPTS:
            text = _read(name)
            with self.subTest(script=name):
                self.assertEqual(1, text.count("ensure_share_secret_wiring"))
                # The strict wiring is inside the non-recovery branch, not before
                # the `--recover-backup` branch where a missing live key would
                # stop the restore that is supposed to recreate it.
                self.assertIn(
                    'if [ -z "$RECOVER_BACKUP" ]; then\n'
                    '  ensure_share_secret_wiring\n'
                    'else\n',
                    text.replace("\r\n", "\n"))
                call = text.index("ensure_share_secret_wiring")
                recovery = text.index('log "explicit recovery: restoring')
                self.assertLess(call, recovery)

    def test_install_reuses_the_effective_share_key_wiring(self):
        # install.sh is the idempotent install/upgrade entry: it must resolve
        # the running console's key before `current` moves and reuse the shared
        # wiring instead of creating a second key at the console-state path.
        install = _read("install.sh")
        common = _read("lib/common.sh")
        self.assertIn("ensure_share_secret_wiring install", install)
        self.assertNotIn(
            'write_if_absent "$UM_CONSOLE_VAR/share-secret.key"', install)
        self.assertLess(
            install.index('CURRENT_TARGET="$(readlink -f "$UM_PREFIX/current")"'),
            install.index(
                'ln -sfn "$UM_RELEASES/$VERSION" "$UM_PREFIX/current.tmp.$$"'))
        self.assertLess(
            install.index(
                '$TARGET/um-cloud-deploy/config/console.env.example'),
            install.index("ensure_share_secret_wiring install"))
        self.assertIn("create_share_secret_file", common)
        self.assertIn('"$mode" = "install"', common)

    def test_a_version_switch_moves_the_operator_bin_entry(self):
        # $UM_PREFIX/bin is what the broker unit and operators execute.  It must
        # move with "current" in both directions.
        update = _read("update.sh")
        rollback = _read("rollback.sh")
        for text in (update, rollback):
            self.assertIn('repoint_bin "$TARGET"', text)
            self.assertIn('repoint_bin "$CURRENT_TARGET"', text)
        self.assertLess(
            update.index('atomic_symlink "$TARGET" "$CURRENT_LINK"'),
            update.index('repoint_bin "$TARGET"'))

    def test_the_update_wiring_keeps_the_effective_release_relative_key(self):
        # An older deployment has no console.env line, so the running console's
        # actual default is <current>/data/share-secret.key.  Creating a new key
        # at the console-state path would silently make existing rows
        # undecryptable (R5).
        common = _read("lib/common.sh")
        self.assertIn('legacy="${CURRENT_TARGET:-}/data/share-secret.key"',
                      common)
        self.assertIn("share_secret_configured_path", common)
        self.assertIn("refusing to create a different key", common)

    def test_explicit_recovery_uses_this_scripts_release(self):
        # The old current release's backup_restore.py does not know
        # --share-secret-owner/--share-secret-group and its
        # deployment_restore does not require the share-secret key (R6).
        for name in SHELL_SCRIPTS:
            text = _read(name)
            self.assertIn('RELEASE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"', text)
            self.assertIn('run_recovery "$RELEASE_ROOT"', text)
            self.assertNotIn('run_recovery "$CURRENT_TARGET"', text)


class SwitchFailureCompensationTests(unittest.TestCase):
    """R10: an incomplete restore must not reopen the edge or claim success.

    Each script's shipped tail (from the unit-state capture to the end) runs with
    the real ``common.sh`` helpers (``atomic_symlink``, ``repoint_bin``,
    ``unit_active``, ``health_gate``) and the real restore/compensation control
    flow.  Doubles sit only at external I/O: ``ln``/``mv``/``readlink`` (a
    simulated link store), ``systemctl``, ``brokerctl``, ``curl`` and the port
    probes.  A test can fail one specific link move (current restore, bin switch,
    bin restore), one service start or stop, the console port close, or the cloudflared edge
    start/stop without replacing the logic under test.  The harness touches no
    unit, link, service or deployment path.
    """

    MARKER = "CLOUDFLARED_WAS_ACTIVE=0"
    PRELUDE = r'''
set -euo pipefail
. "$DEPLOY_LIB/common.sh"
log() { printf 'log %s\n' "$*" >> "$TRACE"; printf 'log %s\n' "$*" >&2; }
die() { printf 'die %s %s\n' "$1" "${2:-70}" >> "$TRACE"; exit "${2:-70}"; }
ACTIVE_STATE=" ${ACTIVE_UNITS:-} "
STARTED_UNITS=""
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$TRACE"
  local verb="" unit="" arg
  for arg in "$@"; do
    case "$arg" in
      -*) ;;
      *) if [ -z "$verb" ]; then verb="$arg"; else unit="$arg"; fi ;;
    esac
  done
  if [ "$verb" = "is-active" ]; then
    if [ "$unit" = "cloudflared.service" ] && [ "${CLOUDFLARED_STILL_ACTIVE:-0}" = "1" ]; then
      return 0
    fi
    case " $ACTIVE_STATE " in
      *" $unit "*) return 0 ;;
    esac
    return 3
  fi
  if [ "$verb" = "start" ]; then
    if [ -n "${FAIL_UNIT_START:-}" ] && [ "$unit" = "$FAIL_UNIT_START" ]; then
      return 1
    fi
    if [ "${FAIL_EDGE_START:-0}" = "1" ] && [ "$unit" = "cloudflared.service" ]; then
      EDGE_START_FAILED=1
      return 1
    fi
    ACTIVE_STATE="$ACTIVE_STATE$unit "
    STARTED_UNITS="$STARTED_UNITS$unit "
    return 0
  fi
  if [ "$verb" = "stop" ]; then
    if [ -n "${FAIL_UNIT_STOP_AFTER_START:-}" ] && \
       [ "$unit" = "$FAIL_UNIT_STOP_AFTER_START" ]; then
      case " $STARTED_UNITS " in
        *" $unit "*) return 1 ;;
      esac
    fi
    if [ "${FAIL_EDGE_STOP:-0}" = "1" ] && [ "$unit" = "cloudflared.service" ] && \
       [ "${EDGE_START_FAILED:-0}" = "1" ]; then
      return 1
    fi
    # Simulate a unit whose stop reports success while the unit stays active.
    # A script that trusts brokerctl's exit instead of checking the unit would
    # wrongly treat this broker as stopped.
    if [ "${BROKER_UNIT_STOP_NOOP:-0}" = "1" ] && \
       [ "$unit" = "um-share-broker.service" ]; then
      case " $STARTED_UNITS " in
        *" $unit "*) return 0 ;;
      esac
    fi
    local next="" u
    for u in $ACTIVE_STATE; do
      [ "$u" = "$unit" ] && continue
      next="$next $u"
    done
    ACTIVE_STATE="$next "
    return 0
  fi
  return 0
}
brokerctl() {
  printf 'brokerctl %s\n' "$*" >> "$TRACE"
  local arg
  for arg in "$@"; do
    case "$arg" in
      start-explicit) [ "${FAIL_BROKERCTL_START:-0}" = "1" ] && return 1 ;;
      stop) [ "${FAIL_BROKER_STOP:-0}" = "1" ] && return 1 ;;
    esac
  done
  return 0
}
brokerctl_release() {
  printf 'brokerctl-release %s\n' "$*" >> "$TRACE"
  return "${FAIL_DRAIN:-0}"
}
install() {
  printf 'install %s\n' "$*" >> "$TRACE"
  local operands=() arg
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -m|-o|-g) shift 2 ;;
      -*) shift ;;
      *) operands+=("$1"); shift ;;
    esac
  done
  [ "${#operands[@]}" -eq 2 ] || return 1
  command mkdir -p "$(dirname "${operands[1]}")"
  command cp "${operands[0]}" "${operands[1]}"
  return 0
}
run_recovery() {
  printf 'recovery %s\n' "$*" >> "$TRACE"
  [ "${FAIL_RECOVERY:-0}" = "1" ] && return 1
  return 0
}
wait_port_open() {
  printf 'wait-open %s %s\n' "$1" "$2" >> "$TRACE"
  [ "${FAIL_PORT:-}" = "$2" ] && return 1
  return 0
}
wait_port_closed() {
  printf 'wait-closed %s %s\n' "$1" "$2" >> "$TRACE"
  if [ "${FAIL_CLOSE_AFTER_START:-}" = "$2" ]; then
    case " $STARTED_UNITS " in
      *" um-share-console-python.service "*) return 1 ;;
    esac
  fi
  return 0
}
curl() {
  local url="" arg
  for arg in "$@"; do
    case "$arg" in
      http*) url="$arg" ;;
    esac
  done
  local n=0
  [ -f "$PROBE_COUNT" ] && n="$(command cat "$PROBE_COUNT")"
  n=$((n + 1))
  printf '%s\n' "$n" > "$PROBE_COUNT"
  printf 'curl %s\n' "$url" >> "$TRACE"
  case "$url" in
    *:8096/*) if [ "$n" -le "${FAIL_CONSOLE_PROBES:-0}" ]; then printf '000'; return 0; fi ;;
    *:8100/*) if [ "$n" -le "${FAIL_GATEWAY_PROBES:-0}" ]; then printf '000'; return 0; fi ;;
  esac
  printf '200'
}
link_key() { printf '%s' "$1" | command tr '/' '_'; }
link_get() {
  local file
  file="$LINK_STORE/$(link_key "$1")"
  [ -f "$file" ] || return 1
  command cat "$file"
}
link_put() {
  local file
  file="$LINK_STORE/$(link_key "$1")"
  printf '%s\n' "$2" > "$file"
}
link_del() { command rm -f "$LINK_STORE/$(link_key "$1")"; }
ln() {
  printf 'ln %s\n' "$*" >> "$TRACE"
  local operands=() arg
  for arg in "$@"; do case "$arg" in -*) ;; *) operands+=("$arg") ;; esac; done
  [ "${#operands[@]}" -eq 2 ] || return 1
  link_put "${operands[1]}" "${operands[0]}"
}
mv() {
  printf 'mv %s\n' "$*" >> "$TRACE"
  local operands=() arg
  for arg in "$@"; do case "$arg" in -*) ;; *) operands+=("$arg") ;; esac; done
  [ "${#operands[@]}" -eq 2 ] || return 1
  local src="${operands[0]}" dst="${operands[1]}" target=""
  target="$(link_get "$src" 2>/dev/null || true)"
  printf 'mv-resolve %s -> %s target=%s\n' "$src" "$dst" "$target" >> "$TRACE"
  if [ "$dst" = "$CURRENT_LINK" ] && [ "${FAIL_CURRENT_RESTORE:-0}" = "1" ] && \
     [ "$target" = "$CURRENT_TARGET" ]; then
    return 72
  fi
  if [ "$dst" = "$UM_PREFIX/bin" ] && [ "${FAIL_BIN_SWITCH:-0}" = "1" ] && \
     [ "$target" = "$TARGET/um-cloud-deploy/bin" ]; then
    return 74
  fi
  if [ "$dst" = "$UM_PREFIX/bin" ] && [ "${FAIL_BIN_SWITCH:-0}" = "1" ] && \
     [ "$target" = "$TARGET/um-cloud-deploy/bin" ]; then
    return 74
  fi
  if [ "$dst" = "$UM_PREFIX/bin" ] && [ "${FAIL_BIN_RESTORE:-0}" = "1" ] && \
     [ "$target" = "$CURRENT_TARGET/um-cloud-deploy/bin" ]; then
    return 74
  fi
  link_put "$dst" "$target"
  link_del "$src"
}
readlink() {
  local follow=0 arg last=""
  for arg in "$@"; do
    case "$arg" in
      -f) follow=1 ;;
      -*) ;;
      *) last="$arg" ;;
    esac
  done
  local target
  target="$(link_get "$last")" || return 1
  if [ "$follow" -eq 1 ]; then
    local next=""
    while next="$(link_get "$target" 2>/dev/null)"; do
      target="$next"
    done
  fi
  printf '%s\n' "$target"
}
'''

    def _run(self, name, bin_is_directory=False, active=None, recover="",
             env_extra=None):
        text = _read(name).replace("\r\n", "\n")
        tail = text[text.index(self.MARKER):]
        if active is None:
            active = ("cloudflared.service", "um-share-console-python.service",
                      "um-share-broker.service")
        with tempfile.TemporaryDirectory(prefix="switch-failure-") as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            releases = root / "releases"
            for release in ("old", "new"):
                units = releases / release / "um-cloud-deploy" / "systemd"
                units.mkdir(parents=True)
                # A release now ships the display unit too: install_units()
                # installs broker, console and display by default, so a fixture
                # release that omits it fails with "systemd unit missing from
                # release" instead of exercising the switch order.
                for unit in ("um-share-broker.service",
                             "um-share-console-python.service",
                             "um-share-display.service"):
                    (units / unit).write_text(
                        (DEPLOY / "systemd" / unit).read_text(
                            encoding="utf-8"),
                        encoding="utf-8", newline="\n")
                (releases / release / "um-cloud-deploy" / "bin").mkdir(
                    parents=True)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            (systemd_dir / "cloudflared.service").write_text(
                "[Unit]\nDescription=cloudflared\n", encoding="utf-8")
            (systemd_dir / "um-share-console-python.service").write_text(
                (DEPLOY / "systemd" / "um-share-console-python.service")
                .read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            store = root / "links"
            store.mkdir()
            if bin_is_directory:
                (prefix / "bin").mkdir(parents=True)
            trace = root / "trace"
            trace.write_text("", encoding="utf-8")
            probe_count = root / "probe-count"
            script = root / "tail.sh"
            script.write_text(self.PRELUDE + tail, encoding="utf-8",
                              newline="\n")
            current_link = _posix(prefix / "current")
            previous_link = _posix(prefix / "previous")
            old_target = _posix(releases / "old")
            new_target = _posix(releases / "new")
            env = dict(
                os.environ,
                DEPLOY_LIB=_posix(DEPLOY / "lib"),
                TRACE=_posix(trace),
                LINK_STORE=_posix(store),
                PROBE_COUNT=_posix(probe_count),
                ACTIVE_UNITS=" ".join(active),
                CURRENT_LINK=current_link,
                PREVIOUS_LINK=previous_link,
                CURRENT_TARGET=old_target,
                TARGET=new_target,
                CURRENT_VERSION="old",
                VERSION="new",
                TARGET_VERSION="new",
                RELEASE_ROOT=new_target,
                RECOVER_BACKUP=recover,
                RECOVER_SECURITY_STATE=recover,
                NO_EDGE="0",
                DRAIN_BUDGET="900",
                DRAIN_MAX="90",
                HEALTH_PORT="8100",
                CURRENT_HEALTH_PORT="8100",
                UM_PREFIX=_posix(prefix),
                UM_SYSTEMD_DIR=_posix(systemd_dir),
                UM_HEALTH_TIMEOUT_S="1",
                EX_OK="0",
                EX_HEALTH="68",
                EX_ROLLBACK="69",
                EX_SOFTWARE="70",
                EX_OSERR="71",
                EX_OSFILE="72",
                EX_CANTCREAT="73",
                EX_IOERR="74",
                EX_TEMPFAIL="75")
            if env_extra:
                env.update(env_extra)
            proc = subprocess.run([BASH, _posix(script)], capture_output=True,
                                  text=True, env=env, timeout=60)
            return SimpleNamespace(
                returncode=proc.returncode,
                stderr=proc.stderr,
                trace=trace.read_text(encoding="utf-8").splitlines(),
                current=self._link(store, current_link),
                bin_target=self._link(store, _posix(prefix / "bin")),
                current_link=current_link,
                old_target=old_target,
                new_target=new_target,
                old_bin=old_target + "/um-cloud-deploy/bin",
                new_bin=new_target + "/um-cloud-deploy/bin")

    @staticmethod
    def _link(store, posix_path):
        key = posix_path.replace("/", "_")
        file = store / key
        return file.read_text(encoding="utf-8").strip() \
            if file.exists() else None

    @staticmethod
    def _indexes(trace, prefix):
        return [i for i, line in enumerate(trace) if line.startswith(prefix)]

    def _assert_no_edge(self, result):
        self.assertNotIn("systemctl start cloudflared.service", result.trace,
                         "the public edge was opened although the restore was "
                         "incomplete")

    @staticmethod
    def _claims_restored(trace):
        # update.sh says "previous release restored"; rollback.sh names the
        # version.  Both must only appear when links, units and cloudflared is back.
        return any("previous release" in line and "restored" in line
                   for line in trace)

    def test_compensation_success_restores_links_units_and_edge(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={"FAIL_BIN_SWITCH": "1"})
                self.assertEqual(74, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current,
                                 "current stayed on the new release")
                self.assertEqual(result.old_bin, result.bin_target,
                                 "the operator bin entry stayed on the new "
                                 "release")
                new_lines = self._indexes(
                    result.trace, "ln -sfn %s " % result.new_bin)
                old_lines = self._indexes(
                    result.trace, "ln -sfn %s " % result.old_bin)
                self.assertTrue(new_lines,
                                "the new operator bin entry was never moved")
                self.assertTrue(old_lines,
                                "the old operator bin entry was not restored")
                self.assertLess(max(new_lines), min(old_lines),
                                "the old operator bin entry was restored "
                                "before the failure")
                self.assertIn("systemctl start um-share-broker.service",
                              result.trace)
                self.assertIn("brokerctl start-explicit", result.trace)
                self.assertIn("systemctl start um-share-console-python.service",
                              result.trace)
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertFalse(
                    any("actual residue" in line for line in result.trace))
                self.assertTrue(
                    self._claims_restored(result.trace),
                    "a full restore was not reported")

    def test_a_failed_current_restore_keeps_the_edge_stopped(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={
                    "FAIL_BIN_SWITCH": "1", "FAIL_CURRENT_RESTORE": "1"})
                self.assertEqual(69, result.returncode, result.stderr)
                self.assertEqual(result.new_target, result.current,
                                 "current was not left on the new release")
                self._assert_no_edge(result)
                residue = [line for line in result.trace
                           if "actual residue" in line]
                self.assertEqual(3, len(residue), result.trace)
                self.assertTrue(any("um-share-broker.service active=" in line
                                for line in residue), result.trace)
                self.assertIn(result.current_link, residue[0])
                self.assertIn(result.new_target, residue[0])
                self.assertFalse(
                    self._claims_restored(result.trace),
                    "an incomplete restore was reported as restored")
                self.assertTrue(
                    any("not restored" in line for line in result.trace),
                    "the link failure was not reported")

    def test_a_failed_bin_restore_keeps_the_edge_stopped(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={
                    "FAIL_CONSOLE_PROBES": "1", "FAIL_BIN_RESTORE": "1"})
                self.assertEqual(69, result.returncode, result.stderr)
                self.assertEqual(result.new_bin, result.bin_target,
                                 "the operator bin entry was not left on "
                                 "the new release")
                self._assert_no_edge(result)
                residue = [line for line in result.trace
                           if "actual residue" in line]
                self.assertEqual(3, len(residue), result.trace)
                self.assertTrue(any("um-share-broker.service active=" in line
                                for line in residue), result.trace)
                self.assertIn(result.new_bin, residue[0])
                self.assertFalse(
                    self._claims_restored(result.trace))

    def test_a_failed_service_restore_keeps_the_edge_stopped(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={
                    "FAIL_BIN_SWITCH": "1",
                    "FAIL_UNIT_START": "um-share-broker.service"})
                self.assertEqual(69, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current)
                self.assertEqual(result.old_bin, result.bin_target)
                self._assert_no_edge(result)
                self.assertTrue(
                    any("manual intervention" in line
                        for line in result.trace),
                    "a failed service restore was not reported")
                self.assertFalse(
                    self._claims_restored(result.trace))

    def test_units_inactive_before_the_switch_are_not_started(self):
        # The normal compensation path must start only the units that were
        # active before the switch.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="normal"):
                result = self._run(name, active=("cloudflared.service",),
                                   env_extra={"FAIL_BIN_SWITCH": "1"})
                self.assertEqual(74, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current)
                self.assertEqual(result.old_bin, result.bin_target)
                self.assertNotIn("systemctl start um-share-broker.service",
                                 result.trace)
                self.assertNotIn("brokerctl start-explicit", result.trace)
                self.assertNotIn("systemctl start um-share-console-python.service",
                                 result.trace)
                self.assertEqual([], self._indexes(result.trace, "wait-open "))
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertTrue(
                    self._claims_restored(result.trace))
        # The health-fallback rollback must not start or wait for an
        # originally inactive unit either.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="health-fallback"):
                result = self._run(name, active=("cloudflared.service",), env_extra={
                    "FAIL_CONSOLE_PROBES": "1"})
                self.assertEqual(68, result.returncode, result.stderr)
                starts = self._indexes(result.trace,
                                      "systemctl start um-share-")
                stops = self._indexes(
                    result.trace,
                    "systemctl stop um-share-console-python.service")
                waits = self._indexes(result.trace, "wait-open ")
                self.assertTrue(starts)
                self.assertTrue(stops)
                self.assertLess(max(starts), max(stops),
                                "a rollback started an originally "
                                "inactive unit")
                self.assertTrue(all(index < max(stops) for index in waits),
                                "a rollback waited for an originally "
                                "inactive unit")
                self.assertIn("systemctl start cloudflared.service", result.trace)

    def test_explicit_recovery_failure_restores_or_asks_for_intervention(self):
        # The recovery itself failed: the current release's links, units and the
        # edge must come back, and the failure must not claim a restore.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="restored"):
                result = self._run(name, recover="/tmp/backup.umbak",
                                   env_extra={"FAIL_RECOVERY": "1"})
                self.assertEqual(70, result.returncode, result.stderr)
                self.assertTrue(
                    any(line.startswith("recovery ")
                        for line in result.trace))
                self.assertIn("systemctl start um-share-broker.service",
                              result.trace)
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertFalse(
                    self._claims_restored(result.trace))
        # A failed recovery followed by an incomplete compensation must stay
        # non-zero with the edge stopped.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="incomplete"):
                result = self._run(name, recover="/tmp/backup.umbak",
                                   env_extra={
                                       "FAIL_RECOVERY": "1",
                                       "FAIL_UNIT_START":
                                           "um-share-broker.service"})
                self.assertEqual(69, result.returncode, result.stderr)
                self._assert_no_edge(result)
                self.assertTrue(
                    any("manual intervention" in line
                        for line in result.trace))

    def test_health_fallback_failure_keeps_the_edge_stopped(self):
        # The rollback after a failed health gate could not restart a unit.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="unit"):
                result = self._run(name, env_extra={
                    "FAIL_CONSOLE_PROBES": "1",
                    "FAIL_UNIT_START": "um-share-console-python.service"})
                self.assertEqual(69, result.returncode, result.stderr)
                self.assertTrue(
                    any("health gate" in line for line in result.trace))
                self._assert_no_edge(result)
                self.assertTrue(
                    any("manual intervention" in line
                        for line in result.trace))
                self.assertFalse(
                    self._claims_restored(result.trace))
        # The release and the units are back but cloudflared did not start: the
        # public entry stays stopped and the exit is non-zero.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name, path="cloudflared.service"):
                result = self._run(name, env_extra={
                    "FAIL_CONSOLE_PROBES": "1",
                    "FAIL_EDGE_START": "1"})
                self.assertEqual(71, result.returncode, result.stderr)
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertIn("systemctl stop cloudflared.service", result.trace)
                self.assertFalse(
                    self._claims_restored(result.trace))

    def test_an_unconfirmed_stop_in_the_health_fallback_keeps_the_edge_closed(self):
        # R10-a: the forward switch starts both units; a failed health gate must
        # confirm they are stopped before restoring the links and reopening
        # cloudflared.  An unconfirmed console stop, an open share entry, or an unconfirmed
        # broker stop leaves the links on the new release, the edge closed and
        # the actual residue readable.
        for name in SHELL_SCRIPTS:
            for label, extra in (
                    ("console-stop", {
                        "FAIL_UNIT_STOP_AFTER_START":
                            "um-share-console-python.service"}),
                    ("broker-stop", {
                        "FAIL_BROKER_STOP": "1",
                        "FAIL_UNIT_STOP_AFTER_START":
                            "um-share-broker.service"})):
                with self.subTest(script=name, case=label):
                    env_extra = {"FAIL_CONSOLE_PROBES": "1"}
                    env_extra.update(extra)
                    result = self._run(name, env_extra=env_extra)
                    self.assertEqual(69, result.returncode, result.stderr)
                    self._assert_no_edge(result)
                    self.assertFalse(
                        self._claims_restored(result.trace),
                        "an incomplete stop was reported as restored")
                    self.assertEqual(
                        result.new_target, result.current,
                        "the release link was restored although a unit "
                        "stop was unconfirmed")
                    self.assertEqual(
                        result.new_bin, result.bin_target,
                        "the operator bin entry was restored although a "
                        "unit stop was unconfirmed")
                    residue = [line for line in result.trace
                               if "actual residue" in line]
                    self.assertEqual(3, len(residue), result.trace)
                    self.assertTrue(
                        any("um-share-broker.service active=" in line
                            for line in residue), result.trace)
                    self.assertIn(result.current_link, residue[0])
                    self.assertIn(result.new_target, residue[0])
                    self.assertTrue(
                        any("did not stop" in line or
                            "still listening" in line
                            for line in result.trace),
                        "the unconfirmed stop was not reported")

    def test_the_broker_unit_is_stopped_even_when_it_was_inactive_before(self):
        # R10-a follow-up: the CLI stop only stops the 8100 host child, not the
        # broker unit.  start_switch starts the unit regardless of its prior
        # state, so a broker that was inactive before the switch must still be
        # stopped as a unit before the previous release is restored; otherwise
        # restore_units skips it and leaves it active while cloudflared reopens.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(
                    name, active=("cloudflared.service", "um-share-console-python.service"),
                    env_extra={"FAIL_CONSOLE_PROBES": "1"})
                self.assertEqual(68, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current)
                cli_stops = [
                    i for i, line in enumerate(result.trace)
                    if line.startswith("brokerctl ")
                    and line.rstrip().endswith(" stop")]
                unit_stops = self._indexes(
                    result.trace,
                    "systemctl stop um-share-broker.service")
                self.assertTrue(cli_stops, result.trace)
                self.assertTrue(
                    unit_stops,
                    "the broker unit started by the forward switch was not "
                    "stopped")
                self.assertLess(max(cli_stops), max(unit_stops),
                                "the broker unit stop did not follow the CLI "
                                "stop")
                broker_starts = self._indexes(
                    result.trace,
                    "systemctl start um-share-broker.service")
                self.assertTrue(broker_starts, result.trace)
                self.assertLess(
                    max(broker_starts), max(unit_stops),
                    "the broker unit was restarted after its stop although it "
                    "was inactive before the switch")
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertTrue(self._claims_restored(result.trace))

    def test_a_broker_unit_that_survives_its_stop_fails_the_rollback(self):
        # The test double returns success for the broker unit stop but keeps the
        # unit active.  brokerctl's successful stop must not be mistaken for a
        # stopped unit: the script must report the unconfirmed unit, keep the
        # links on the new release and leave cloudflared closed.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={
                    "FAIL_CONSOLE_PROBES": "1",
                    "BROKER_UNIT_STOP_NOOP": "1"})
                self.assertEqual(69, result.returncode, result.stderr)
                self._assert_no_edge(result)
                self.assertEqual(result.new_target, result.current)
                self.assertEqual(result.new_bin, result.bin_target)
                self.assertFalse(
                    self._claims_restored(result.trace),
                    "a still-active broker was reported as restored")
                self.assertTrue(
                    any("still active after its stop" in line
                        for line in result.trace),
                    "the still-active broker unit was not reported")

    def test_an_unconfirmed_edge_stop_is_reported_as_possibly_open(self):
        # R10-b: a failed cloudflared start does not prove the unit is down.  When
        # the compensating stop fails or the unit is still active, the scripts
        # must say the public entry may still be open instead of claiming it
        # stopped.
        for name in SHELL_SCRIPTS:
            for label, extra in (
                    ("stop-failed", {"FAIL_EDGE_STOP": "1"}),
                    ("still-active", {"CLOUDFLARED_STILL_ACTIVE": "1"})):
                with self.subTest(script=name, case=label):
                    env_extra = {"FAIL_CONSOLE_PROBES": "1",
                                 "FAIL_EDGE_START": "1"}
                    env_extra.update(extra)
                    result = self._run(name, env_extra=env_extra)
                    self.assertEqual(71, result.returncode, result.stderr)
                    starts = self._indexes(
                        result.trace, "systemctl start cloudflared.service")
                    stops = self._indexes(
                        result.trace, "systemctl stop cloudflared.service")
                    self.assertTrue(starts, result.trace)
                    self.assertTrue(stops, result.trace)
                    self.assertLess(max(starts), max(stops),
                                    "cloudflared was not stopped after the "
                                    "failed start")
                    self.assertFalse(
                        self._claims_restored(result.trace),
                        "a restore with an unconfirmed edge stop was "
                        "reported as restored")
                    self.assertTrue(
                        any("may still be open" in line
                            for line in result.trace),
                        "the unconfirmed edge stop was not reported")
                    self.assertFalse(
                        any("cloudflared" in line and "stays stopped" in line
                            for line in result.trace),
                        "an unconfirmed cloudflared stop was still reported "
                        "as stopped")

    def test_no_edge_failure_messages_do_not_claim_the_edge_is_stopped(self):
        # --no-edge means this run never managed Caddy, so a failed
        # compensation must ask the operator to verify the public entry instead
        # of asserting it is stopped.
        for name in SHELL_SCRIPTS:
            for label, extra in (
                    ("link-failure", {
                        "FAIL_BIN_SWITCH": "1",
                        "FAIL_CURRENT_RESTORE": "1"}),
                    ("unit-failure", {
                        "FAIL_BIN_SWITCH": "1",
                        "FAIL_UNIT_START": "um-share-broker.service"}),
                    ("rollback-switch", {
                        "FAIL_CONSOLE_PROBES": "1",
                        "FAIL_UNIT_STOP_AFTER_START":
                            "um-share-console-python.service"})):
                with self.subTest(script=name, case=label):
                    env_extra = {"NO_EDGE": "1"}
                    env_extra.update(extra)
                    result = self._run(name, env_extra=env_extra)
                    self.assertEqual(69, result.returncode, result.stderr)
                    # die() writes to the trace in this harness; log() to stderr.
                    output = "\n".join([result.stderr] + result.trace)
                    self.assertNotIn(
                        "cloudflared stays stopped", output,
                        "an unmanaged edge was reported as stopped")
                    self.assertIn("not managed by this run", output)
                    self.assertIn("may still be serving", output)
                    self.assertIn("verify the public entry", output)

    def test_a_managed_edge_failure_still_reports_the_edge_stopped(self):
        # Without --no-edge the run stopped Caddy itself, so the existing
        # "cloudflared stays stopped" claim remains correct.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={
                    "FAIL_BIN_SWITCH": "1",
                    "FAIL_UNIT_START": "um-share-broker.service"})
                self.assertEqual(69, result.returncode, result.stderr)
                output = "\n".join([result.stderr] + result.trace)
                self.assertIn("cloudflared stays stopped", output)
                self.assertNotIn("not managed by this run", output)

    def test_health_fallback_success_reports_the_previous_release(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name,
                                   env_extra={"FAIL_CONSOLE_PROBES": "1"})
                self.assertEqual(68, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current)
                self.assertEqual(result.old_bin, result.bin_target)
                self.assertIn("systemctl start cloudflared.service", result.trace)
                self.assertTrue(
                    self._claims_restored(result.trace))

    def test_a_drain_failure_does_not_switch_and_reopens_the_edge(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, env_extra={"FAIL_DRAIN": "75"})
                self.assertEqual(75, result.returncode, result.stderr)
                self.assertEqual(result.old_target, result.current)
                self.assertEqual(result.old_bin, result.bin_target)
                self.assertEqual([], self._indexes(
                    result.trace, "ln -sfn %s " % result.new_target))
                self.assertEqual([], self._indexes(
                    result.trace, "ln -sfn %s " % result.new_bin))
                self.assertIn("systemctl start cloudflared.service", result.trace)

    def test_a_non_symlink_bin_entry_is_refused_before_stopping_services(self):
        # repoint_bin() rejects a real file/directory at $UM_PREFIX/bin with
        # die().  That precondition is static, so the switch must refuse it while
        # cloudflared, the broker and the console are still running instead of
        # discovering it after the drain and the version switch.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name, bin_is_directory=True)
                self.assertEqual(73, result.returncode, result.stderr)
                self.assertTrue(
                    any("not a symlink" in line for line in result.trace),
                    result.trace)
                self.assertNotIn("systemctl stop cloudflared.service", result.trace,
                                 "services were stopped before the refusal")
                self.assertEqual(
                    [], [line for line in result.trace
                         if line.startswith("ln -sfn ")],
                    "a link moved before the refusal")


class ConsolePortResolutionTests(unittest.TestCase):
    """Expand-contract (2026-11-14): ops probes follow the deployed console port.

    The console moved 18096 -> 8096.  install/update/rollback must read the
    deployed console.env instead of hardcoding one side, so a deployment that
    still carries the legacy 18096 explicitly keeps working for one release
    cycle.  A missing line means the new 8096 default; an invalid value fails
    closed instead of silently probing a different port.
    """

    def _console_port(self, text):
        with tempfile.TemporaryDirectory(prefix="console-port-") as tmp:
            etc = Path(tmp)
            if text is not None:
                (etc / "console.env").write_text(text, encoding="utf-8")
            etc_arg = str(etc)
            if os.name == "nt":
                converted = subprocess.run(
                    ["cygpath", "-u", etc_arg],
                    capture_output=True, text=True)
                self.assertEqual(0, converted.returncode, converted.stderr)
                etc_arg = converted.stdout.strip()
            script = (
                "set -euo pipefail\n"
                '. "$DEPLOY_LIB/common.sh"\n'
                "console_port\n")
            env = dict(os.environ, DEPLOY_LIB=str(DEPLOY / "lib"),
                        UM_ETC=etc_arg)
            return subprocess.run([BASH, "-c", script], capture_output=True,
                                  text=True, env=env)

    def test_missing_console_env_uses_the_new_default(self):
        proc = self._console_port(None)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("8096", proc.stdout.strip())

    def test_the_deployed_new_port_is_used(self):
        proc = self._console_port("USAGESRV_PORT=8096\n")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("8096", proc.stdout.strip())

    def test_the_legacy_port_stays_an_explicit_value(self):
        proc = self._console_port("USAGESRV_PORT=18096\n")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("18096", proc.stdout.strip())

    def test_an_invalid_port_fails_closed(self):
        for value in ("", "abc", "0", "65536", "8096 "):
            with self.subTest(value=value):
                proc = self._console_port("USAGESRV_PORT=%s\n" % value)
                self.assertNotEqual(0, proc.returncode, proc.stdout)
                self.assertIn("USAGESRV_PORT must be 1..65535", proc.stderr)

    def test_whitespace_around_the_assignment_is_accepted(self):
        # systemd EnvironmentFile allows whitespace around '='; the ops helper
        # must read the same value the service will.
        proc = self._console_port("USAGESRV_PORT = 8096\n")
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("8096", proc.stdout.strip())


if __name__ == "__main__":
    unittest.main()
