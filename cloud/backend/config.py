# -*- coding: utf-8 -*-
"""Cloud composition-root configuration (E19-1-11).

One explicit JSON document owns every listener, state path, secret file and
backend fact the cloud application needs. Validation is fail-closed:

* every field is required (no production default is silently applied) and
  unknown fields are refused;
* a missing or invalid field raises ``CloudConfigError`` carrying the field
  name, so ``--check-config`` can exit 2 without binding a port, creating a
  directory or starting a child process;
* paths must be absolute and the backend gateway directory must be the
  in-package ``chat2api`` source directory;
* the listener host must be loopback; the application port and the backend
  port are explicit real ports and never the same.

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
# The composition-root package (``ai-gateway``); the backend gateway source
# must be the ``chat2api`` directory INSIDE this package.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# E19-1-06 step 3: the real deployment listeners. A cloud fixture must never
# bind one of these.
FORBIDDEN_PORTS = frozenset({8000, 8001, 8100, 8110, 18096, 5901})

# E19-1-06 step 5: the real state/profile/secret paths of the deployment.
# The set matches the local guard: ``backend_profile_dir`` is handed to the 8110
# child as ``--profile`` and ``backend_service_log`` is where it writes its log,
# so pointing either at the in-package real state would touch the operator's live
# data. ``profile/`` and ``chat2api/chat2api.log`` were missing here, which made
# those two paths pass the guard.
REAL_STATE_ROOTS = (
    PACKAGE_ROOT / "data",
    PACKAGE_ROOT / "profile",
    PACKAGE_ROOT / "chat2api" / ".edge-profile",
    PACKAGE_ROOT / "chat2api" / "token.json",
    PACKAGE_ROOT / "chat2api" / "chat2api.log",
)

# E19-1-06 step 9: the two runtime modes. ``fixture`` means the composition
# root owns the broker child; ``production`` means systemd owns it and
# ``--start-broker`` must refuse.
RUNTIME_MODES = frozenset(("fixture", "production"))

# The complete field order is the contract: ``from_mapping`` rejects anything
# outside this set instead of silently ignoring a typo.
PATH_FIELDS = (
    "state_root",
    "home_dir",
    "config_dir",
    "log_dir",
    "lock_dir",
    "pid_file",
    "control_token_file",
    "share_run_root",
    "share_store_file",
    "share_secret_key_file",
    "broker_socket",
    "broker_key_file",
    "projection_file",
    "attribution_key_file",
    "instance_key_file",
    "registration_file",
    "instance_prefix",
    "revoke_state_file",
    "host_state_dir",
    "host_pid_file",
    "broker_dir",
    "backend_dir",
    "backend_gateway_dir",
    "backend_token_file",
    "backend_profile_dir",
    "backend_service_log",
    "backend_usage_log",
    "backend_control_token_file",
    "backend_reauth_slot_file",
    "backend_python",
)
REQUIRED_FIELDS = (
    "schema_version",
    "host",
    "port",
    "state_root",
    "home_dir",
    "config_dir",
    "log_dir",
    "lock_dir",
    "pid_file",
    "control_token_file",
    "share_run_root",
    "share_store_file",
    "share_secret_key_file",
    "console_state_file",
    "console_lock_dir",
    "share_management_config",
    "broker_socket",
    "broker_key_file",
    "projection_file",
    "attribution_key_file",
    "instance_key_file",
    "registration_file",
    "instance_prefix",
    "revoke_state_file",
    "host_state_dir",
    "host_pid_file",
    "broker_dir",
    "backend_dir",
    "backend_port",
    "backend_base_url",
    "backend_gateway_dir",
    "backend_token_file",
    "backend_profile_dir",
    "backend_service_log",
    "backend_usage_log",
    "backend_control_token_file",
    "backend_reauth_slot_file",
    "backend_python",
    "backend_start_timeout_s",
    "backend_stop_timeout_s",
)
# E19-1-06 steps 8/11: optional explicit fields. ``runtime_mode`` defaults to
# ``production`` (systemd owns the broker); the fixture passes ``fixture``.
# ``supervision_watch_dir``/``supervision_lock_dir`` default to
# ``host_state_dir``/``console_lock_dir``; ``price_table_file`` is absent in a
# deployment that has no local price table (the summary read face then answers
# 503 summary_unavailable instead of inventing prices).
OPTIONAL_FIELDS = (
    "runtime_mode",
    "supervision_watch_dir",
    "supervision_lock_dir",
    "price_table_file",
    # standards-config-12: these three were REQUIRED and written to the
    # process environment, but the cloud import closure has NO consumer — the
    # only readers are in ``usagesrv.py``, which R19-1 excludes from the
    # cloud root. A required field nobody consumes is not wiring: it only made
    # the operator believe the cloud had taken over the console state DB/lock
    # dir/share-management config. They stay accepted (an existing
    # ``console.env`` keeps working) and are never applied to the environment.
    "console_state_file",
    "console_lock_dir",
    "share_management_config",
)
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

# Process-wide environment the cloud root applies BEFORE constructing the
# ShareManager/BrokerClientAdapter (E19-1-11). These are the same names the
# deployment modules already read, so an env-driven consumer and the explicit
# CloudConfig can never disagree.
PROCESS_ENV_FIELDS = (
    ("SHARE_STORE_FILE", "share_store_file"),
    ("CHAT2API_SHARE_SECRET_KEY_FILE", "share_secret_key_file"),
    ("CONSOLE_SHARE_RUN_ROOT", "share_run_root"),
    ("CONSOLE_SHARE_ATTRIBUTION_KEY_FILE", "attribution_key_file"),
    ("CONSOLE_SHARE_BACKEND_DIR", "backend_dir"),
    ("CONSOLE_SHARE_BACKEND_PORT", "backend_port"),
    ("CONSOLE_SHARE_BACKEND_BASE_URL", "backend_base_url"),
    ("PORT", "port"),
    ("BASE_URL", "backend_base_url"),
)


class CloudConfigError(ValueError):
    """One invalid or missing configuration field (named, never guessed)."""

    def __init__(self, field, message):
        super().__init__("%s: %s" % (field, message))
        self.field = field
        self.message = message


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _require_text(raw, field, *, absolute=False):
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise CloudConfigError(field, "must be a non-empty string")
    if absolute and not os.path.isabs(value):
        raise CloudConfigError(field, "must be an absolute path")
    return value


def _require_port(raw, field):
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudConfigError(field, "must be an integer")
    if not 1 <= value <= 65535:
        raise CloudConfigError(field, "must be in 1..65535")
    return value


def _require_timeout(raw, field):
    value = raw.get(field)
    if not _is_number(value) or value <= 0:
        raise CloudConfigError(field, "must be a positive number")
    return float(value)


def _require_url(raw, field):
    value = _require_text(raw, field)
    if not value.startswith(("http://", "https://")):
        raise CloudConfigError(field, "must be an http(s) URL")
    return value


def _is_in_package(path):
    """True only for a path inside the composition-root package."""
    try:
        return Path(path).resolve().is_relative_to(PACKAGE_ROOT)
    except (OSError, ValueError):
        return False


def _reject_production_port(port, field):
    """E19-1-06 step 3: no configured listener may be a production port."""
    if port in FORBIDDEN_PORTS:
        raise CloudConfigError(
            field,
            "must not be a production port (%d is in %s)"
            % (port, ", ".join(str(p) for p in sorted(FORBIDDEN_PORTS))))


def _reject_real_state(path, field):
    """E19-1-06 step 5: no configured path may be real deployment state.

    standards-config-14: Windows ``Path.resolve()`` silently turns a NUL byte
    into a space instead of raising, so the guard inspected a DIFFERENT string
    than the one ``open()`` later uses (which raises ValueError). On platforms
    where ``resolve()`` does raise, the old ``except: return`` ACCEPTED the
    path. A NUL byte is refused up front and a resolution failure is a
    refusal, never a pass.
    """
    if isinstance(path, str) and "\x00" in path:
        raise CloudConfigError(field, "must not contain a NUL byte")
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError) as exc:
        raise CloudConfigError(
            field, "is not a usable path: %s" % exc) from None
    for real in REAL_STATE_ROOTS:
        try:
            real_resolved = real.resolve()
        except (OSError, ValueError):
            continue
        if resolved == real_resolved or real_resolved in resolved.parents:
            raise CloudConfigError(
                field,
                "must not name the real deployment state %s" % real_resolved)


def _reject_non_loopback_upstream(value, field):
    """E19-1-06 step 4: the backend upstream must be loopback and non-prod.

    A remote host would let the cloud console drive an upstream outside this
    machine; a production port would let it drive the private chain. Both are
    refused with the field name so ``--check-config`` exits 2.
    """
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = int(parts.port or (443 if parts.scheme == "https" else 80))
    except ValueError as exc:
        raise CloudConfigError(field, "has an invalid URL") from exc
    if host not in LOOPBACK_HOSTS:
        raise CloudConfigError(
            field, "must name a loopback host (got %r)" % host)
    if port in FORBIDDEN_PORTS:
        raise CloudConfigError(
            field,
            "must not name a production port (%d is in %s)"
            % (port, ", ".join(str(p) for p in sorted(FORBIDDEN_PORTS))))


def _home_roots(values):
    """The directories a ``home_dir`` may equal or live under.

    Whitelist, NOT "must not be under Path.home()": on Windows every
    ``tempfile.mkdtemp()`` lands under ``Path.home()`` (measured), so the latter
    rule would make every legitimate fixture illegal.
    """
    import tempfile
    roots = []
    configured = []
    for name in ("state_root", "log_dir", "lock_dir", "config_dir"):
        value = values.get(name)
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


def _reject_real_home(home_dir, values, field="home_dir"):
    """E19-1-06 step 6: ``home_dir`` must be an explicit isolated root.

    Same reachable rule as the local root (see local/backend/config.py): a
    whitelist of configured roots + the system temp dir + their common parent,
    and a refusal for any ancestor of those roots. ``Path.home()`` is NOT used:
    the entry point sets ``HOME`` to the isolated home first.
    """
    try:
        resolved = Path(home_dir).resolve()
    except (OSError, ValueError):
        raise CloudConfigError(field, "must be an absolute path") from None
    for real in (PACKAGE_ROOT / "data", PACKAGE_ROOT / "profile"):
        try:
            real_resolved = real.resolve()
        except (OSError, ValueError):
            continue
        if resolved == real_resolved:
            raise CloudConfigError(
                field,
                "must not be the real deployment directory %s" % real_resolved)
    roots = _home_roots(values)
    ancestor = any(resolved != root and resolved in root.parents
                   for root in roots)
    if ancestor:
        raise CloudConfigError(
            field,
            "must not be an ancestor of the isolated roots %s"
            % ", ".join(str(r) for r in roots))
    if not any(resolved == root or root in resolved.parents
               for root in roots):
        raise CloudConfigError(
            field,
            "must equal or live under one of %s (an explicit isolated root)"
            % ", ".join(str(r) for r in roots))


@dataclass(frozen=True)
class CloudConfig:
    """The complete, explicit cloud composition-root configuration."""

    schema_version: int
    host: str
    port: int
    state_root: str
    home_dir: str
    config_dir: str
    log_dir: str
    lock_dir: str
    pid_file: str
    control_token_file: str
    share_run_root: str
    share_store_file: str
    share_secret_key_file: str
    console_state_file: str | None
    console_lock_dir: str | None
    share_management_config: str | None
    broker_socket: str
    broker_key_file: str
    projection_file: str
    attribution_key_file: str
    instance_key_file: str
    registration_file: str
    instance_prefix: str
    revoke_state_file: str
    host_state_dir: str
    host_pid_file: str
    broker_dir: str
    backend_dir: str
    backend_port: int
    backend_base_url: str
    backend_gateway_dir: str
    backend_token_file: str
    backend_profile_dir: str
    backend_service_log: str
    backend_usage_log: str
    backend_control_token_file: str
    backend_reauth_slot_file: str
    backend_python: str
    backend_start_timeout_s: float
    backend_stop_timeout_s: float
    runtime_mode: str
    supervision_watch_dir: str
    supervision_lock_dir: str
    price_table_file: str | None

    @property
    def backend_price_table_file(self):
        """The price document the 8110 child prices its ledger with.

        ``price_table_file`` is optional for the SUMMARY read face (no price
        table means ``503 summary_unavailable``), but the 8110 child always
        needs ONE explicit identity path. An omitted value maps to the explicit
        state root — never to the in-package ``data/prices.json`` default the
        child would otherwise pick on its own (standards-config-05).
        """
        if self.price_table_file is not None:
            return self.price_table_file
        return os.path.join(self.host_state_dir, "prices.json")

    @classmethod
    def from_file(cls, path):
        """Load and validate one explicit JSON document (no other I/O)."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise CloudConfigError("<config>", "unreadable: %s" % exc)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise CloudConfigError("<config>", "invalid JSON: %s" % exc)
        if not isinstance(payload, dict):
            raise CloudConfigError("<config>", "must be a JSON object")
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, raw):
        """Validate a mapping; every failure names the offending field."""
        if not isinstance(raw, dict):
            raise CloudConfigError("<config>", "must be a JSON object")
        unknown = sorted(set(raw) - set(ALL_FIELDS))
        if unknown:
            raise CloudConfigError(unknown[0], "unknown field")
        missing = [name for name in REQUIRED_FIELDS if name not in raw]
        if missing:
            raise CloudConfigError(missing[0], "missing required field")

        schema_version = raw.get("schema_version")
        if (isinstance(schema_version, bool)
                or schema_version != SCHEMA_VERSION):
            raise CloudConfigError(
                "schema_version", "must be %d" % SCHEMA_VERSION)
        host = _require_text(raw, "host")
        if host not in LOOPBACK_HOSTS:
            raise CloudConfigError("host", "must be a loopback host")
        port = _require_port(raw, "port")
        backend_port = _require_port(raw, "backend_port")
        if backend_port == port:
            raise CloudConfigError(
                "backend_port", "must differ from the application port")
        values = {}
        for field in PATH_FIELDS:
            values[field] = _require_text(raw, field, absolute=True)
        if os.path.basename(values["backend_gateway_dir"].rstrip("/\\")) \
                != "chat2api":
            raise CloudConfigError(
                "backend_gateway_dir",
                "must be the in-package chat2api source directory")
        if not _is_in_package(values["backend_gateway_dir"]):
            raise CloudConfigError(
                "backend_gateway_dir",
                "must be the in-package chat2api source directory "
                "(outside the package is not a runtime dependency)")
        backend_base_url = _require_url(raw, "backend_base_url")
        # E19-1-06 steps 3-6: the isolation guards. They run at config
        # validation time so ``--check-config`` exits 2 WITHOUT binding a
        # port, creating a directory or starting a child process.
        _reject_production_port(port, "port")
        _reject_production_port(backend_port, "backend_port")
        _reject_non_loopback_upstream(
            backend_base_url, "backend_base_url")
        for field, value in values.items():
            _reject_real_state(value, field)
        _reject_real_home(values["home_dir"], values)
        runtime_mode = raw.get("runtime_mode", "production")
        if runtime_mode not in RUNTIME_MODES:
            raise CloudConfigError(
                "runtime_mode",
                "must be one of %s" % ", ".join(sorted(RUNTIME_MODES)))
        supervision_watch_dir = raw.get("supervision_watch_dir",
                                         values["host_state_dir"])
        # standards-config-12: ``console_lock_dir`` is no longer a consumed
        # field; the supervision lock root defaults to the cloud's OWN
        # ``lock_dir``.
        supervision_lock_dir = raw.get("supervision_lock_dir",
                                      values["lock_dir"])
        supervision_watch_dir = _require_text(
            {"supervision_watch_dir": supervision_watch_dir},
            "supervision_watch_dir", absolute=True)
        supervision_lock_dir = _require_text(
            {"supervision_lock_dir": supervision_lock_dir},
            "supervision_lock_dir", absolute=True)
        price_table_file = raw.get("price_table_file")
        if price_table_file is not None:
            price_table_file = _require_text(
                raw, "price_table_file", absolute=True)
        # standards-config-12: these three are ACCEPTED for an existing
        # ``console.env`` but are NOT consumed by the cloud closure, so they are
        # optional and are never applied to the process environment. A value the
        # operator supplies is still validated as an absolute path.
        legacy_console_paths = {}
        for field in ("console_state_file", "console_lock_dir",
                      "share_management_config"):
            value = raw.get(field)
            if value is not None:
                value = _require_text(raw, field, absolute=True)
                _reject_real_state(value, field)
            legacy_console_paths[field] = value
        # E19-1-06 step 5: the OPTIONAL path fields are real write/read
        # targets too (the supervision watch dir is where the desired state is
        # written; the price table is read for the summary), so they pass the
        # SAME real-state guard as the required paths.
        for field, value in (
                ("supervision_watch_dir", supervision_watch_dir),
                ("supervision_lock_dir", supervision_lock_dir),
                ("price_table_file", price_table_file)):
            if value is not None:
                _reject_real_state(value, field)
        return cls(
            schema_version=schema_version,
            host=host,
            port=port,
            backend_port=backend_port,
            backend_base_url=backend_base_url,
            backend_start_timeout_s=_require_timeout(
                raw, "backend_start_timeout_s"),
            backend_stop_timeout_s=_require_timeout(
                raw, "backend_stop_timeout_s"),
            runtime_mode=runtime_mode,
            supervision_watch_dir=supervision_watch_dir,
            supervision_lock_dir=supervision_lock_dir,
            price_table_file=price_table_file,
            console_state_file=legacy_console_paths["console_state_file"],
            console_lock_dir=legacy_console_paths["console_lock_dir"],
            share_management_config=legacy_console_paths[
                "share_management_config"],
            **values,
        )

    def process_env(self):
        """The explicit environment the composition root applies in-process.

        Applied BEFORE constructing the ShareManager/BrokerClientAdapter so
        env-driven modules and the CloudConfig fields can never disagree.
        """
        return {name: str(getattr(self, field))
                for name, field in PROCESS_ENV_FIELDS}

    def broker_args(self):
        """The broker's OWN required CLI tuple (``share/broker_ops.py:2605-2640``).

        The flags are the ones ``share/broker_ops.py::linux_main`` actually
        defines. The previous tuple (``--socket``/``--control-key-file``/
        ``--share-store``/``--share-management-config``/``--share-run-root``/
        ``--backend-port``/``--pid-file``) shared not one spelling with the real
        parser, so ``--start-broker`` could never start the broker.

        ``--host-command`` is deliberately ABSENT: it is a REMAINDER carrying the
        8100 gateway argv, which belongs to the caller (the systemd unit in
        production, the chain fixture in verification), never to this config.

        ``tests/r19/cloud_fixture.broker_argv()`` returns the same tuple from the
        same field names; a test pins the two together.
        """
        return (
            "share/broker_ops.py",
            "--socket-path", self.broker_socket,
            "--key-file", self.broker_key_file,
            "--instance-key-file", self.instance_key_file,
            "--registration-file", self.registration_file,
            "--instance-socket-prefix", self.instance_prefix,
            "--projection-path", self.projection_file,
            "--broker-dir", self.broker_dir,
            "--host-pid-file", self.host_pid_file,
            "--state-dir", self.host_state_dir,
        )

    def validate_config_dir(self, config_path):
        """``config_dir`` must be the ``--config`` document's parent.

        standards-config-09: the local entry already refuses a document that
        declares one ``config_dir`` but is read from another. The cloud entry had
        NO such check, so ``config_dir`` could be declared as an unrelated
        directory (e.g. the real user home) and pull ``home_dir`` under it past
        the isolation allow-list.
        """
        try:
            actual = Path(config_path).resolve().parent
            declared = Path(self.config_dir).resolve()
        except (OSError, ValueError) as exc:
            raise CloudConfigError(
                "config_dir", "is not a usable directory: %s" % exc)
        if actual != declared:
            raise CloudConfigError(
                "config_dir",
                "must be the parent directory of --config (%s != %s)"
                % (declared, actual))
        return self

    def runtime_info(self):
        """Non-secret facts printed by ``--check-config``."""
        return {
            "status": "ok",
            "host": self.host,
            "port": self.port,
            "backend_port": self.backend_port,
            "backend_base_url": self.backend_base_url,
            "state_root": self.state_root,
            "home_dir": self.home_dir,
            "runtime_mode": self.runtime_mode,
            "supervision_watch_dir": self.supervision_watch_dir,
            "supervision_lock_dir": self.supervision_lock_dir,
        }
