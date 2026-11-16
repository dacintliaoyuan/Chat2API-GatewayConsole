# -*- coding: utf-8 -*-
"""r17-2: a release switch replaces the running broker host process.

``update.sh``/``rollback.sh`` must stop the broker unit before the ``current``
link moves and start it again only after the new release's unit files are installed
and ``systemctl daemon-reload`` has run.  The installed units must come from the
release that ``current`` points at, so the console unit can no longer pull an
inactive broker back up (r17-1).

The shipped script tails run with the real ``common.sh`` helpers and a fake
``systemctl``/``install``/``brokerctl``.  A fake unit directory stands in for
``/etc/systemd/system``; no real unit, link or service is touched.

Run from ``ai-gateway``:

    python -m unittest tests.test_update_broker_replace
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parents[1]      # ai-gateway/
DEPLOY = HERE / "um-cloud-deploy"
BASH = shutil.which("bash") or "bash"
SHELL_SCRIPTS = ("update.sh", "rollback.sh")
MARKER = "CLOUDFLARED_MANAGED=0"


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


def _posix(path):
    text = str(Path(path).resolve()).replace("\\", "/")
    if len(text) > 1 and text[1:2] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


PRELUDE = r'''
set -euo pipefail
. "$DEPLOY_LIB/common.sh"
log() { printf 'log %s\n' "$*" >> "$TRACE"; printf 'log %s\n' "$*" >&2; }
die() { printf 'die %s %s\n' "$1" "${2:-70}" >> "$TRACE"; exit "${2:-70}"; }
ACTIVE_STATE=" ${ACTIVE_UNITS:-} "
STARTED_UNITS=""
BROKER_GENERATION=0
unit_wants() {
  local file="$UM_SYSTEMD_DIR/$1"
  [ -f "$file" ] || return 0
  command sed -n 's/^Wants=//p' "$file" | command tr '\n' ' '
}
start_unit() {
  local unit="$1" dep
  case " $ACTIVE_STATE " in
    *" $unit "*) return 0 ;;
  esac
  ACTIVE_STATE="$ACTIVE_STATE$unit "
  STARTED_UNITS="$STARTED_UNITS$unit "
  if [ "$unit" = "um-share-broker.service" ]; then
    BROKER_GENERATION=$((BROKER_GENERATION + 1))
  fi
  for dep in $(unit_wants "$unit"); do
    start_unit "$dep"
  done
  return 0
}
systemctl() {
  printf 'systemctl %s\n' "$*" >> "$TRACE"
  if [ "$1" = "daemon-reload" ]; then
    if [ "${FAIL_DAEMON_RELOAD:-0}" = "1" ]; then
      # Fail only the FIRST reload: the recovery reload after reinstalling
      # the previous release's units must still succeed.
      FAIL_DAEMON_RELOAD=0
      return 1
    fi
    return 0
  fi
  if [ "$1" = "show" ]; then
    local prop="" unit="" arg
    shift
    while [ "$#" -gt 0 ]; do
      case "$1" in
        -p) prop="$2"; shift 2 ;;
        --value) shift ;;
        *) unit="$1"; shift ;;
      esac
    done
    local file="$UM_SYSTEMD_DIR/$unit"
    [ -f "$file" ] || return 0
    case "$prop" in
      Wants) command sed -n 's/^Wants=//p' "$file" | command tr '\n' ' ' ;;
      ExecStart)
        if [ -n "${EXEC_START_OVERRIDE:-}" ]; then
          printf '%s' "$EXEC_START_OVERRIDE"
        else
          command sed -n 's/^ExecStart=//p' "$file"
        fi
        ;;
    esac
    return 0
  fi
  local verb="" unit="" arg
  for arg in "$@"; do
    case "$arg" in
      -*) ;;
      *) if [ -z "$verb" ]; then verb="$arg"; else unit="$arg"; fi ;;
    esac
  done
  if [ "$verb" = "is-active" ]; then
    case " $ACTIVE_STATE " in
      *" $unit "*) return 0 ;;
    esac
    return 3
  fi
  if [ "$verb" = "start" ]; then
    if [ -n "${FAIL_UNIT_START:-}" ] && [ "$unit" = "$FAIL_UNIT_START" ]; then
      return 1
    fi
    start_unit "$unit"
    return 0
  fi
  if [ "$verb" = "stop" ]; then
    if [ -n "${FAIL_UNIT_STOP:-}" ] && [ "$unit" = "$FAIL_UNIT_STOP" ]; then
      return 1
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
      start-explicit) BROKER_GENERATION=$((BROKER_GENERATION + 1)) ;;
    esac
  done
  return 0
}
brokerctl_release() { printf 'brokerctl-release %s\n' "$*" >> "$TRACE"; return 0; }
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
  return 0
}
wait_port_open() { printf 'wait-open %s %s\n' "$1" "$2" >> "$TRACE"; return 0; }
wait_port_closed() { printf 'wait-closed %s %s\n' "$1" "$2" >> "$TRACE"; return 0; }
health_gate() { printf 'health-gate\n' >> "$TRACE"; return 0; }
curl() { printf '200'; }
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


class BrokerReplaceOrderTests(unittest.TestCase):
    def _run(self, name, *, active=None, managed_edge=True,
             edge_active=False, recover="", env_extra=None):
        text = _read(name).replace("\r\n", "\n")
        tail = text[text.index(MARKER):]
        if active is None:
            active = ("um-share-console.service",
                      "um-share-broker.service")
        if edge_active:
            active = tuple(active) + ("cloudflared.service",)
        with tempfile.TemporaryDirectory(prefix="broker-replace-") as tmp:
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
                             "um-share-console.service",
                             "um-share-display.service"):
                    (units / unit).write_text(
                        (DEPLOY / "systemd" / unit).read_text(
                            encoding="utf-8"),
                        encoding="utf-8", newline="\n")
                (releases / release / "um-cloud-deploy" / "bin").mkdir(
                    parents=True)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            for unit in ("um-share-broker.service",
                         "um-share-console.service",
                         "um-share-display.service"):
                (systemd_dir / unit).write_text(
                    (DEPLOY / "systemd" / unit).read_text(encoding="utf-8"),
                    encoding="utf-8", newline="\n")
            if managed_edge:
                (systemd_dir / "cloudflared.service").write_text(
                    "[Unit]\nDescription=cloudflared\n", encoding="utf-8")
            store = root / "links"
            store.mkdir()
            trace = root / "trace"
            trace.write_text("", encoding="utf-8")
            script = root / "tail.sh"
            script.write_text(PRELUDE + tail, encoding="utf-8", newline="\n")
            current_link = _posix(prefix / "current")
            previous_link = _posix(prefix / "previous")
            old_target = _posix(releases / "old")
            new_target = _posix(releases / "new")
            if name == "update.sh":
                current_target, target = old_target, new_target
                current_version, target_version = "old", "new"
            else:
                current_target, target = new_target, old_target
                current_version, target_version = "new", "old"
            env = dict(
                os.environ,
                DEPLOY_LIB=_posix(DEPLOY / "lib"),
                TRACE=_posix(trace),
                LINK_STORE=_posix(store),
                ACTIVE_UNITS=" ".join(active),
                CURRENT_LINK=current_link,
                PREVIOUS_LINK=previous_link,
                CURRENT_TARGET=current_target,
                TARGET=target,
                CURRENT_VERSION=current_version,
                VERSION=target_version,
                TARGET_VERSION=target_version,
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
                new_bin=new_target + "/um-cloud-deploy/bin",
                systemd_dir=systemd_dir)

    @staticmethod
    def _link(store, posix_path):
        key = posix_path.replace("/", "_")
        file = store / key
        return file.read_text(encoding="utf-8").strip() \
            if file.exists() else None

    @staticmethod
    def _indexes(trace, prefix):
        return [i for i, line in enumerate(trace) if line.startswith(prefix)]

    def test_update_stops_the_broker_before_the_switch(self):
        result = self._run("update.sh")
        self.assertEqual(0, result.returncode, result.stderr)
        switch = self._indexes(
            result.trace, "ln -sfn %s " % result.new_target)
        self.assertTrue(switch, result.trace)
        stops = self._indexes(
            result.trace, "systemctl stop um-share-broker.service")
        starts = self._indexes(
            result.trace, "systemctl start um-share-broker.service")
        self.assertTrue(stops, result.trace)
        self.assertTrue(starts, result.trace)
        self.assertLess(max(stops), min(switch),
                        "the broker unit was not stopped before the switch")
        self.assertGreater(min(starts), max(switch),
                            "the broker unit was started before the switch")
        self.assertGreater(
            min(self._indexes(result.trace, "brokerctl start-explicit")),
            max(switch))
        self.assertEqual(result.new_target, result.current)
        self.assertEqual(result.new_bin, result.bin_target)

    def test_rollback_stops_the_broker_before_the_switch(self):
        result = self._run("rollback.sh")
        self.assertEqual(0, result.returncode, result.stderr)
        switch = self._indexes(
            result.trace, "ln -sfn %s " % result.old_target)
        self.assertTrue(switch, result.trace)
        stops = self._indexes(
            result.trace, "systemctl stop um-share-broker.service")
        starts = self._indexes(
            result.trace, "systemctl start um-share-broker.service")
        self.assertTrue(stops, result.trace)
        self.assertTrue(starts, result.trace)
        self.assertLess(max(stops), min(switch))
        self.assertGreater(min(starts), max(switch))
        self.assertEqual(result.old_target, result.current)
        self.assertEqual(result.old_bin, result.bin_target)

    def test_units_are_installed_before_daemon_reload_and_start(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(name)
                self.assertEqual(0, result.returncode, result.stderr)
                expected_source = (result.new_target if name == "update.sh"
                                   else result.old_target)
                reloads = self._indexes(result.trace, "systemctl daemon-reload")
                self.assertTrue(reloads, result.trace)
                for unit in ("um-share-broker.service",
                             "um-share-console.service"):
                    installs = [
                        i for i, line in enumerate(result.trace)
                        if line.startswith("install ") and
                        line.endswith("/systemd/" + unit)
                    ]
                    self.assertTrue(installs,
                                    "unit %s was not installed" % unit)
                    self.assertLess(max(installs), min(reloads),
                                    "unit %s was installed after "
                                    "daemon-reload" % unit)
                    self.assertIn(expected_source, result.trace[max(installs)],
                                  "the installed unit does not come from "
                                  "the release that current points at")
                starts = self._indexes(
                    result.trace, "systemctl start um-share-broker.service")
                self.assertGreater(min(starts), max(reloads),
                                   "the broker unit was started before "
                                   "daemon-reload")

    def test_a_failed_daemon_reload_restores_previous_units_and_links(self):
        # The unit files are installed before daemon-reload. If the reload
        # fails, the new units are not loaded and the version links must not
        # stay switched: reinstall the previous release's units, reload again,
        # and restore the previous links/units.
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                result = self._run(
                    name, env_extra={"FAIL_DAEMON_RELOAD": "1"})
                self.assertEqual(71, result.returncode, result.stderr)
                expected = (result.old_target if name == "update.sh"
                            else result.new_target)
                self.assertEqual(expected, result.current)
                self.assertEqual(expected + "/um-cloud-deploy/bin",
                                 result.bin_target)
                reloads = self._indexes(result.trace,
                                         "systemctl daemon-reload")
                self.assertGreaterEqual(len(reloads), 2,
                                        "the recovery reload did not run")
                for unit in ("um-share-broker.service",
                             "um-share-console.service"):
                    installs = [
                        i for i, line in enumerate(result.trace)
                        if line.startswith("install ") and
                        line.endswith("/systemd/" + unit)
                    ]
                    self.assertTrue(installs,
                                    "unit %s was not reinstalled" % unit)
                    self.assertIn(expected, result.trace[max(installs)],
                                  "the recovery unit does not come from "
                                  "the restored release")
                    self.assertLess(max(installs), max(reloads))

    def test_the_console_start_does_not_start_the_broker(self):
        with tempfile.TemporaryDirectory(prefix="console-wants-") as tmp:
            root = Path(tmp)
            systemd_dir = root / "systemd"
            systemd_dir.mkdir()
            for unit in ("um-share-broker.service",
                         "um-share-console.service"):
                (systemd_dir / unit).write_text(
                    (DEPLOY / "systemd" / unit).read_text(encoding="utf-8"),
                    encoding="utf-8", newline="\n")
            trace = root / "trace"
            trace.write_text("", encoding="utf-8")
            script = root / "console.sh"
            script.write_text(PRELUDE + "\n"
                               "systemctl start um-share-console.service\n",
                               encoding="utf-8", newline="\n")
            env = dict(os.environ,
                        DEPLOY_LIB=_posix(DEPLOY / "lib"),
                        TRACE=_posix(trace),
                        LINK_STORE=_posix(root / "links"),
                        ACTIVE_UNITS="",
                        UM_SYSTEMD_DIR=_posix(systemd_dir))
            (root / "links").mkdir()
            proc = subprocess.run([BASH, _posix(script)], capture_output=True,
                                  text=True, env=env, timeout=60)
            self.assertEqual(0, proc.returncode, proc.stderr)
            lines = trace.read_text(encoding="utf-8").splitlines()
            self.assertIn("systemctl start um-share-console.service", lines)
            self.assertNotIn("systemctl start um-share-broker.service", lines,
                             "starting the console pulled the broker up")

    def test_an_unmanaged_edge_is_not_stopped_or_started(self):
        result = self._run("update.sh", managed_edge=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("systemctl stop cloudflared.service", result.trace)
        self.assertNotIn("systemctl start cloudflared.service", result.trace)
        self.assertIn("not managed by this deployment", result.stderr)

    def test_an_inactive_edge_is_not_started(self):
        result = self._run("update.sh", edge_active=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("systemctl start cloudflared.service", result.trace)
        self.assertIn("cloudflared.service is inactive", result.stderr)

    def test_an_active_edge_is_stopped_and_restarted(self):
        result = self._run("update.sh", edge_active=True)
        self.assertEqual(0, result.returncode, result.stderr)
        switch = self._indexes(
            result.trace, "ln -sfn %s " % result.new_target)
        stops = self._indexes(
            result.trace, "systemctl stop cloudflared.service")
        starts = self._indexes(
            result.trace, "systemctl start cloudflared.service")
        self.assertTrue(stops, result.trace)
        self.assertTrue(starts, result.trace)
        self.assertLess(max(stops), min(switch))
        self.assertGreater(min(starts), max(switch))

    def test_residue_reports_unit_state_wants_and_budget_args(self):
        result = self._run(
            "update.sh",
            env_extra={"FAIL_BIN_SWITCH": "1",
                       "FAIL_CURRENT_RESTORE": "1",
                       "EXEC_START_OVERRIDE":
                           "--budget-db /tmp/budget.db"})
        self.assertEqual(69, result.returncode, result.stderr)
        output = "\n".join([result.stderr] + result.trace)
        self.assertIn("um-share-broker.service active=", output)
        self.assertIn("Wants=", output)
        self.assertIn("budget args present", output)


if __name__ == "__main__":
    unittest.main()
