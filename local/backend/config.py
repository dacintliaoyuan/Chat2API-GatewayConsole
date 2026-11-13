# -*- coding: utf-8 -*-
"""Local composition-root configuration (E19-1-08).

One explicit JSON document owns every listener, state path, secret file and UM
runtime fact the local application needs. Validation is fail-closed:

* every field is required and unknown fields (top-level or nested) are refused;
* a missing or invalid field raises ``LocalConfigError`` carrying the field name,
  so ``--check-config`` can exit 2 without binding a port, creating a directory
  or starting a child process;
* paths must be absolute, the listener hosts must be loopback, and the application
  port, the UM runtime port and the fake-upstream port are explicit real ports
  that never collide;
* the UM runtime source directory must be the in-package ``chat2api`` directory;
* the UM runtime ``home_dir`` is required and must equal ``LocalConfig.home_dir``
  or live under it: the child's ``HOME``/``USERPROFILE``/``TMPDIR`` come from
  that field, so a value outside the isolated root would place temp files back in the operator's real profile;
* the runtime-control mode is either the explicit ``fixture`` child-process owner
  or the configured ``powershell`` wrapper (which then requires a script).

The module performs no I/O beyond reading the JSON document itself and no
module-level environment reads: the composition root decides what to apply.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1", "localhost"))
SCHEMA_VERSION = 1
# The composition-root package (``ai-gateway``); the UM runtime source must be
# the ``chat2api`` directory INSIDE this package, never another checkout.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# E19-1-04 step 3: the real deployment listeners. A local fixture must never
# bind one of these - an accidental match would take the production service's
# port (8000/8001 private chain, 8100 share gateway, 8110 share backend,
# 18096 cloud console, 5901 whatever the deployment assigns) or steal it.
FORBIDDEN_PORTS = frozenset({8000, 8001, 8100, 8110, 18096, 5901})

# E19-1-04 step 4: the real state/profile/secret paths of the in-package
# deployment. A fixture that names one of these would read or write the operator's
# live data, so any configured path equal to or below one is refused.
REAL_STATE_ROOTS = (
    PACKAGE_ROOT / "data",
    PACKAGE_ROOT / "profile",
    PACKAGE_ROOT / "chat2api" / ".edge-profile",
    PACKAGE_ROOT / "chat2api" / "token.json",
    PACKAGE_ROOT / "chat2api" / "chat2api.log",
)

# E19-1-04 step 6: production runtime wrappers. ``runtime_control.mode``
# ``powershell`` runs an arbitrary script; pointing it at one of these would turn
# a config check into a real 8000/8001 start/stop.
FORBIDDEN_RUNTIME_BASENAMES = frozenset((
    "invoke-gateway-service.ps1",
    "gateway-runtime.ps1",
    "start_chat2api.bat",
    "start_usage_service.bat",
    "recover-sharecontrol.ps1",
    "recover-sharecontrol.bat",
))
FORBIDDEN_RUNTIME_PATTERNS = ("start_*.vbs", "recover-sharecontrol.*")
# The content markers a controlled fixture must never carry: each one is a real
# start/stop action in the production wrappers (``invoke-console-runtime-json.ps1``
# dot-sources ``lib/gateway-runtime.ps1``, which calls ``Start-Process``).
FORBIDDEN_RUNTIME_CONTENT = (
    "Start-Process",
    "Invoke-ConsoleRuntimeStart",
    "gateway-runtime.ps1",
)

# The complete field order is the contract: ``from_mapping`` rejects anything
# outside this set instead of silently ignoring a typo.
PATH_FIELDS = (
    "state_root",
    "home_dir",
    "config_dir",
    "log_dir",
    "lock_dir",
    "pid_file",
    "ledger_file",
    "price_file",
    "session_code_file",
    "control_token_file",
    "console_state_file",
    "model_sync_dir",
)
# Optional explicit paths: when present they must still be absolute, but a
# deployment that has no manual price override file simply omits the field.
OPTIONAL_PATH_FIELDS = ("price_override_file",)
UM_PATH_FIELDS = (
    "source_dir",
    "python",
    "profile_dir",
    "home_dir",
    "token_file",
    "service_log",
    "usage_log",
    "control_token_file",
    "reauth_slot_file",
)
UM_STATE_PATH_FIELDS = (
    "profile_dir",
    "home_dir",
    "token_file",
    "service_log",
    "usage_log",
    "control_token_file",
    "reauth_slot_file",
)
REQUIRED_FIELDS = (
    "schema_version",
    "host",
    "port",
) + PATH_FIELDS + (
    "um_runtime",
    "runtime_control",
)
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_PATH_FIELDS
UM_REQUIRED_FIELDS = UM_PATH_FIELDS + (
    "host",
    "port",
    "base_url",
    "upstream_token",
    "auth_wait_budget_s",
    "request_max_s",
    "start_timeout_s",
    "stop_timeout_s",
)
RUNTIME_CONTROL_REQUIRED_FIELDS = ("mode", "timeout_s")
RUNTIME_CONTROL_FIELDS = RUNTIME_CONTROL_REQUIRED_FIELDS + ("script",)
RUNTIME_CONTROL_MODES = frozenset(("fixture", "powershell"))


class LocalConfigError(ValueError):
    """One invalid or missing configuration field (named, never guessed)."""

    def __init__(self, field, message):
        super().__init__("%s: %s" % (field, message))
        self.field = field
        self.message = message


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_mapping(raw, field):
    value = raw.get(field)
    if not isinstance(value, dict):
        raise LocalConfigError(field, "must be a JSON object")
    return value


def _require_text(raw, field, *, absolute=False):
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LocalConfigError(field, "must be a non-empty string")
    # standards-config-14: a NUL byte passes ``os.path.isabs`` and Windows
    # ``Path.resolve`` silently turns it into a space, so the guard would
    # inspect a DIFFERENT string than the one ``open()`` later uses (which
    # raises ValueError). The external boundary refuses it up front.
    if "\x00" in value:
        raise LocalConfigError(field, "must not contain a NUL byte")
    if absolute and not os.path.isabs(value):
        raise LocalConfigError(field, "must be an absolute path")
    return value


def _require_int(raw, field, *, minimum=None, maximum=None):
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocalConfigError(field, "must be an integer")
    if minimum is not None and value < minimum:
        raise LocalConfigError(field, "must be >= %d" % minimum)
    if maximum is not None and value > maximum:
        raise LocalConfigError(field, "must be <= %d" % maximum)
    return value


def _require_port(raw, field):
    return _require_int(raw, field, minimum=1, maximum=65535)


def _require_timeout(raw, field):
    value = raw.get(field)
    if not _is_number(value) or value <= 0:
        raise LocalConfigError(field, "must be a positive number")
    return float(value)


def _require_url(raw, field):
    value = _require_text(raw, field)
    if not value.startswith(("http://", "https://")):
        raise LocalConfigError(field, "must be an http(s) URL")
    return value


def _is_in_package(path):
    """True only for a path inside the composition-root package."""
    try:
        return Path(path).resolve().is_relative_to(PACKAGE_ROOT)
    except (OSError, ValueError):
        return False


def _url_parts(value, field):
    from urllib.parse import urlsplit

    parts = urlsplit(value)
    if not parts.hostname:
        raise LocalConfigError(field, "must name a host")
    try:
        port = int(parts.port or 80)
    except ValueError as exc:
        raise LocalConfigError(field, "has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise LocalConfigError(field, "has an invalid port")
    return parts, port


def _reject_production_port(port, field):
    """E19-1-04 step 3: no configured listener may be a production port."""
    if port in FORBIDDEN_PORTS:
        raise LocalConfigError(
            field,
            "must not be a production port (%d is in %s)"
            % (port, ", ".join(str(p) for p in sorted(FORBIDDEN_PORTS))))


def _reject_real_state(path, field):
    """E19-1-04 step 4: no configured path may be real deployment state.

    standards-config-14: Windows ``Path.resolve()`` silently turns a NUL byte
    into a space instead of raising, so the guard inspected a DIFFERENT string
    than the one ``open()`` later uses (which raises ValueError). On platforms
    where ``resolve()`` does raise, the old ``except: return`` ACCEPTED the
    path. A NUL byte is refused up front and a resolution failure is a
    refusal, never a pass.
    """
    if isinstance(path, str) and "\x00" in path:
        raise LocalConfigError(field, "must not contain a NUL byte")
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as exc:
        raise LocalConfigError(
            field, "is not a usable path: %s" % exc) from None
    for real in REAL_STATE_ROOTS:
        try:
            real_resolved = real.resolve()
        except (OSError, ValueError):
            continue
        if resolved == real_resolved or real_resolved in resolved.parents:
            raise LocalConfigError(
                field,
                "must not name the real deployment state %s"
                % real_resolved)


def _home_roots(config_like):
    """The directories a ``home_dir`` may equal or live under (E19-1-04 step 5).

    Whitelist, NOT "must not be under Path.home()": on Windows every
    ``tempfile.mkdtemp()`` lands under ``Path.home()`` (measured), so the latter
    rule would make every legitimate fixture illegal.

    Allowed roots, per the plan: each configured state root, the system temp dir,
    and the ONE explicit isolated root those configured roots share (a deployment
    names a root and puts ``state``/``logs``/``locks``/``home`` beside each
    other). The common parent is admitted only when the configured roots actually
    agree on one; the real deployment directories are refused independently by
    ``_reject_real_home`` before this whitelist is consulted.
    """
    import tempfile
    roots = []
    configured = []
    for name in ("state_root", "log_dir", "lock_dir", "config_dir"):
        value = config_like.get(name)
        if isinstance(value, str) and value.strip():
            try:
                configured.append(Path(value).resolve())
            except (OSError, ValueError):
                continue
    roots.extend(configured)
    try:
        roots.append(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        pass
    if configured:
        try:
            common = Path(os.path.commonpath(
                [str(path) for path in configured])).resolve()
        except (OSError, ValueError):
            common = None
        if common is not None and not _is_filesystem_root(common):
            # standards-config-08: when the configured roots sit in different
            # top-level directories their common parent is the filesystem root
            # (/ or a drive root), and every absolute path is "under" it — the
            # whole home guard would then accept the real user home. A root is
            # never a usable allow-list entry.
            roots.append(common)
    return roots


def _is_filesystem_root(path):
    """True for the filesystem root or a bare drive root."""
    try:
        path = Path(path).resolve()
    except (OSError, ValueError):
        return True
    return path == Path(path.anchor) or path.parent == path


def _reject_real_home(home_dir, config_like, field="home_dir"):
    """E19-1-04 step 5: ``home_dir`` must be an explicit isolated root.

    Two independent rules, neither of which trusts ``Path.home()``:

    1. it must equal or live UNDER one of the allowed roots (see
       ``_home_roots``) — the real user home is normally the PARENT of the
       system temp dir, so it is refused here;
    2. it must not be an ANCESTOR of any allowed root, so pointing the isolated
       home at ``C:/Users/<name>`` (whose ``AppData/Local/Temp`` is the temp
       root) is refused even though the first rule could be read as satisfied.

    ``Path.home()`` is deliberately NOT used: the entry point sets ``HOME`` to the
    configured isolated home before this runs, so ``Path.home()`` would report the
    fixture's own directory and the comparison would be a tautology.
    """
    try:
        resolved = Path(home_dir).resolve()
    except (OSError, ValueError):
        raise LocalConfigError(field, "must be an absolute path") from None
    for real in (PACKAGE_ROOT / "data", PACKAGE_ROOT / "profile"):
        try:
            real_resolved = real.resolve()
        except (OSError, ValueError):
            continue
        if resolved == real_resolved:
            raise LocalConfigError(
                field,
                "must not be the real deployment directory %s"
                % real_resolved)
    roots = _home_roots(config_like)
    inside = any(resolved == root or root in resolved.parents
                 for root in roots)
    ancestor = any(resolved != root and resolved in root.parents
                   for root in roots)
    if ancestor:
        raise LocalConfigError(
            field,
            "must not be an ancestor of the isolated roots %s"
            % ", ".join(str(r) for r in roots))
    if not inside:
        raise LocalConfigError(
            field,
            "must equal or live under one of %s (an explicit isolated root)"
            % ", ".join(str(r) for r in roots))


def _require_um_home(um_runtime, home_dir):
    """E19-1-04 step 8: the UM child HOME is the application's isolated home.

    ``um_runtime.home_dir`` is required (a missing one is refused by
    ``from_mapping``), must be absolute, and must equal ``home_dir`` or live
    under it. The child gets ``HOME``/``USERPROFILE``/``TMPDIR`` from it, so a
    value outside the isolated root would place temp files
    back in the operator's real profile.
    """
    try:
        resolved = Path(um_runtime.home_dir).resolve()
        root = Path(home_dir).resolve()
    except (OSError, ValueError):
        raise LocalConfigError(
            "um_runtime.home_dir", "must be an absolute path") from None
    if resolved != root and root not in resolved.parents:
        raise LocalConfigError(
            "um_runtime.home_dir",
            "must equal or live under home_dir (%s)" % root)


def _reject_production_runtime(script, field):
    """E19-1-04 step 2 (F32): only an in-package controlled fixture is allowed.

    ``service_runtime.PowerShellServiceRuntime`` runs the configured script with
    ``subprocess.run([..., "-File", script, "-Action", action])``, so the value is
    a real start/stop action, not a passive configuration field. The contract in
    ``local/README.md`` and in the plan is:

    * the script must live INSIDE the deployment package (an out-of-package copy
      of the real 8000/8001 starter used to be ACCEPTED here);
    * its content must not contain ``Start-Process``,
      ``Invoke-ConsoleRuntimeStart`` or a dot-source of ``gateway-runtime.ps1``.

    Both directions are fail-closed: an unreadable script is refused instead of
    being assumed harmless.
    """
    if script is None:
        return
    basename = os.path.basename(str(script).rstrip("/\\")).lower()
    if basename in FORBIDDEN_RUNTIME_BASENAMES:
        raise LocalConfigError(
            field,
            "must not be the production runtime wrapper %r" % basename)
    import fnmatch
    for pattern in FORBIDDEN_RUNTIME_PATTERNS:
        if fnmatch.fnmatch(basename, pattern):
            raise LocalConfigError(
                field,
                "must not be the production runtime wrapper %r" % basename)
    try:
        resolved = Path(script).resolve()
    except (OSError, ValueError):
        raise LocalConfigError(
            field, "is not a usable script path") from None
    if not _is_in_package(resolved):
        raise LocalConfigError(
            field,
            "must live inside the deployment package (an out-of-package copy "
            "of a production starter is never a test fixture)")
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise LocalConfigError(
            field, "is not readable: %s" % exc) from None
    for marker in FORBIDDEN_RUNTIME_CONTENT:
        if marker in text:
            raise LocalConfigError(
                field,
                "must not contain %r (a real service start/stop action)"
                % marker)


@dataclass(frozen=True)
class UmRuntimeConfig:
    """The explicit local UM runtime (chat2api child process) configuration."""

    source_dir: str
    python: str
    host: str
    port: int
    profile_dir: str
    home_dir: str
    token_file: str
    service_log: str
    usage_log: str
    control_token_file: str
    reauth_slot_file: str
    base_url: str
    upstream_token: str
    auth_wait_budget_s: int
    request_max_s: int
    start_timeout_s: float
    stop_timeout_s: float

    @classmethod
    def from_mapping(cls, raw):
        unknown = sorted(set(raw) - set(UM_REQUIRED_FIELDS))
        if unknown:
            raise LocalConfigError(
                "um_runtime.%s" % unknown[0], "unknown field")
        missing = [name for name in UM_REQUIRED_FIELDS if name not in raw]
        if missing:
            raise LocalConfigError(
                "um_runtime.%s" % missing[0], "missing required field")

        values = {}
        try:
            for field in UM_PATH_FIELDS:
                values[field] = _require_text(raw, field, absolute=True)
            if os.path.basename(values["source_dir"].rstrip("/\\")) \
                    != "chat2api":
                raise LocalConfigError(
                    "source_dir",
                    "must be the in-package chat2api source directory")
            if not _is_in_package(values["source_dir"]):
                raise LocalConfigError(
                    "source_dir",
                    "must be the in-package chat2api source directory "
                    "(outside the package is not a runtime dependency)")
            host = _require_text(raw, "host")
            if host not in LOOPBACK_HOSTS:
                raise LocalConfigError("host", "must be a loopback host")
            base_url = _require_url(raw, "base_url")
            parts, base_port = _url_parts(base_url, "base_url")
            if (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
                raise LocalConfigError(
                    "base_url", "must be a loopback host")
            port = _require_port(raw, "port")
            if base_port == port:
                raise LocalConfigError(
                    "base_url", "must differ from the UM runtime port")
            return cls(
                source_dir=values["source_dir"],
                python=values["python"],
                host=host,
                port=port,
                profile_dir=values["profile_dir"],
                home_dir=values["home_dir"],
                token_file=values["token_file"],
                service_log=values["service_log"],
                usage_log=values["usage_log"],
                control_token_file=values["control_token_file"],
                reauth_slot_file=values["reauth_slot_file"],
                base_url=base_url,
                upstream_token=_require_text(raw, "upstream_token"),
                auth_wait_budget_s=_require_int(
                    raw, "auth_wait_budget_s", minimum=1, maximum=180),
                request_max_s=_require_int(raw, "request_max_s", minimum=1),
                start_timeout_s=_require_timeout(raw, "start_timeout_s"),
                stop_timeout_s=_require_timeout(raw, "stop_timeout_s"),
            )
        except LocalConfigError as exc:
            raise LocalConfigError("um_runtime." + exc.field,
                                  exc.message) from exc


@dataclass(frozen=True)
class RuntimeControlConfig:
    """How the local composition root controls its UM runtime."""

    mode: str
    script: str | None
    timeout_s: float

    @classmethod
    def from_mapping(cls, raw):
        unknown = sorted(set(raw) - set(RUNTIME_CONTROL_FIELDS))
        if unknown:
            raise LocalConfigError(
                "runtime_control.%s" % unknown[0], "unknown field")
        missing = [name for name in RUNTIME_CONTROL_REQUIRED_FIELDS
                   if name not in raw]
        if missing:
            raise LocalConfigError(
                "runtime_control.%s" % missing[0], "missing required field")
        try:
            mode = _require_text(raw, "mode")
            if mode not in RUNTIME_CONTROL_MODES:
                raise LocalConfigError(
                    "mode",
                    "must be one of %s" % ", ".join(sorted(
                        RUNTIME_CONTROL_MODES)))
            script = raw.get("script")
            if script is not None:
                if not isinstance(script, str) or not script.strip():
                    raise LocalConfigError(
                        "script",
                        "must be a non-empty absolute path or null")
                if not os.path.isabs(script):
                    raise LocalConfigError(
                        "script", "must be an absolute path")
            if mode == "powershell" and script is None:
                raise LocalConfigError(
                    "script", "is required for the powershell mode")
            return cls(
                mode=mode,
                script=script,
                timeout_s=_require_timeout(raw, "timeout_s"),
            )
        except LocalConfigError as exc:
            raise LocalConfigError("runtime_control." + exc.field,
                                   exc.message) from exc


@dataclass(frozen=True)
class LocalConfig:
    """The complete, explicit local composition-root configuration."""

    schema_version: int
    host: str
    port: int
    state_root: str
    home_dir: str
    config_dir: str
    log_dir: str
    lock_dir: str
    pid_file: str
    ledger_file: str
    price_file: str
    price_override_file: str | None
    session_code_file: str
    control_token_file: str
    console_state_file: str
    model_sync_dir: str
    um_runtime: UmRuntimeConfig
    runtime_control: RuntimeControlConfig

    @classmethod
    def from_file(cls, path):
        """Load and validate one explicit JSON document (no other I/O)."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalConfigError("<config>", "unreadable: %s" % exc)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise LocalConfigError("<config>", "invalid JSON: %s" % exc)
        if not isinstance(payload, dict):
            raise LocalConfigError("<config>", "must be a JSON object")
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, raw):
        """Validate a mapping; every failure names the offending field."""
        if not isinstance(raw, dict):
            raise LocalConfigError("<config>", "must be a JSON object")
        unknown = sorted(set(raw) - set(ALL_FIELDS))
        if unknown:
            raise LocalConfigError(unknown[0], "unknown field")
        missing = [name for name in REQUIRED_FIELDS if name not in raw]
        if missing:
            raise LocalConfigError(missing[0], "missing required field")

        schema_version = raw.get("schema_version")
        if (isinstance(schema_version, bool)
                or schema_version != SCHEMA_VERSION):
            raise LocalConfigError(
                "schema_version", "must be %d" % SCHEMA_VERSION)
        host = _require_text(raw, "host")
        if host not in LOOPBACK_HOSTS:
            raise LocalConfigError("host", "must be a loopback host")
        port = _require_port(raw, "port")
        values = {}
        for field in PATH_FIELDS:
            values[field] = _require_text(raw, field, absolute=True)
        price_override_file = raw.get("price_override_file")
        if price_override_file is not None:
            price_override_file = _require_text(
                raw, "price_override_file", absolute=True)
        um_runtime = UmRuntimeConfig.from_mapping(
            _require_mapping(raw, "um_runtime"))
        runtime_control = RuntimeControlConfig.from_mapping(
            _require_mapping(raw, "runtime_control"))
        if um_runtime.port == port:
            raise LocalConfigError(
                "um_runtime.port",
                "must differ from the application port")
        _parts, upstream_port = _url_parts(
            um_runtime.base_url, "um_runtime.base_url")
        if upstream_port in (port, um_runtime.port):
            raise LocalConfigError(
                "um_runtime.base_url",
                "must differ from the application and UM runtime ports")
        # E19-1-04 steps 3-6: the isolation guards. They run at config
        # validation time so ``--check-config`` exits 2 WITHOUT binding a port,
        # creating a directory or starting a child process.
        _reject_production_port(port, "port")
        _reject_production_port(um_runtime.port, "um_runtime.port")
        _reject_production_port(upstream_port, "um_runtime.base_url")
        for field, value in values.items():
            _reject_real_state(value, field)
        if price_override_file is not None:
            _reject_real_state(price_override_file, "price_override_file")
        for field in UM_STATE_PATH_FIELDS:
            _reject_real_state(
                getattr(um_runtime, field), "um_runtime.%s" % field)
        _reject_real_home(values["home_dir"], values)
        # E19-1-04 step 8: the UM child HOME must be the SAME isolated root the
        # application uses (equal or below it). Anything else would let the
        # child resolve temp files outside the isolation.
        _require_um_home(um_runtime, values["home_dir"])
        # standards-config-06 / spec-local-006: the ledger the summary READS
        # and the ledger the 8110 child WRITES are one identity, not two
        # independent paths. A mismatch made the summary read a file the child
        # never wrote and report a quiet zero.
        try:
            same_ledger = (Path(values["ledger_file"]).resolve()
                           == Path(um_runtime.usage_log).resolve())
        except (OSError, ValueError):
            same_ledger = False
        if not same_ledger:
            raise LocalConfigError(
                "um_runtime.usage_log",
                "must be the same file as ledger_file (the summary reads "
                "the ledger the UM runtime writes)")
        _reject_production_runtime(
            runtime_control.script, "runtime_control.script")
        return cls(
            schema_version=schema_version,
            host=host,
            port=port,
            price_override_file=price_override_file,
            um_runtime=um_runtime,
            runtime_control=runtime_control,
            **values,
        )

    def runtime_info(self):
        """Non-secret facts printed by ``--check-config``."""
        return {
            "status": "ok",
            "host": self.host,
            "port": self.port,
            "um_host": self.um_runtime.host,
            "um_port": self.um_runtime.port,
            "um_base_url": self.um_runtime.base_url,
            "state_root": self.state_root,
            "home_dir": self.home_dir,
            "runtime_control": self.runtime_control.mode,
        }

    def validate_config_dir(self, config_path):
        """``config_dir`` must be the ``--config`` document's parent.

        A config that names one directory but is read from another would write
        its session code / price document somewhere the operator did not name, so
        the mismatch is refused at ``--check-config`` time (exit 2).
        """
        try:
            actual = Path(config_path).resolve().parent
            declared = Path(self.config_dir).resolve()
        except (OSError, ValueError) as exc:
            raise LocalConfigError(
                "config_dir", "is not a usable directory: %s" % exc)
        if actual != declared:
            raise LocalConfigError(
                "config_dir",
                "must be the parent directory of --config (%s != %s)"
                % (declared, actual))
        return self

    def config_paths(self):
        """Every configured state path (never the interpreter/source dir)."""
        paths = {field: getattr(self, field) for field in PATH_FIELDS}
        if self.price_override_file is not None:
            paths["price_override_file"] = self.price_override_file
        for field in UM_STATE_PATH_FIELDS:
            paths["um_runtime.%s" % field] = getattr(self.um_runtime, field)
        return paths
