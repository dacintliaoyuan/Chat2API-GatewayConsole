# -*- coding: utf-8 -*-
"""E19-1-06: explicit broker loader + explicit BackendConfig.

The production console must not reach back into ``usagesrv`` to build the share
broker: the composition root configures a loader explicitly
(``configure_broker_loader``). With no loader configured the honest refusal is
``share_broker_unwired`` and the deployment pointer stays in the message — the
module never imports ``usagesrv`` on its own.

The dedicated 8110 backend must also be assemblable from an explicit
``BackendConfig``: every path/port/base URL comes from the config object and the
explicit-config path never reads the process environment. A missing config keeps
the historical environment-driven behavior byte for byte.

Every test here is offline: no deployment file, no process, no port, no network.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
AI_GATEWAY = Path(__file__).resolve().parents[1]

from console import share_production as prod  # noqa: E402
from console.operations import OperationError  # noqa: E402


class _NoEnvironment:
    """An ``os.environ`` stand-in that fails on ANY key lookup.

    ``dict(mapping)`` copies through ``keys()`` + ``__getitem__``, so an empty
    iteration is allowed; any actual configuration lookup raises. The explicit
    ``BackendConfig`` path must never ask the environment for a value.
    """

    def keys(self):
        return ()

    def __iter__(self):
        return iter(())

    def __getitem__(self, key):
        raise AssertionError(
            "explicit BackendConfig path read os.environ[%r]" % (key,))

    def get(self, key, default=None):
        raise AssertionError(
            "explicit BackendConfig path read os.environ.get(%r)" % (key,))

    def __contains__(self, key):
        raise AssertionError(
            "explicit BackendConfig path probed os.environ for %r" % (key,))


class _LoaderResetMixin(unittest.TestCase):
    def setUp(self):
        self._saved_loader = prod._BROKER_LOADER
        self.addCleanup(prod.configure_broker_loader, self._saved_loader)


class BrokerLoaderTests(_LoaderResetMixin):
    """The loader is the ONE explicit broker construction seam."""

    def test_no_loader_raises_honestly_and_never_imports_usagesrv(self):
        prod.configure_broker_loader(None)
        with mock.patch.dict(sys.modules):
            sys.modules.pop("usagesrv", None)
            with self.assertRaises(OperationError) as caught:
                prod._default_broker()
            self.assertNotIn("usagesrv", sys.modules)
        exc = caught.exception
        self.assertEqual(exc.code, "share_broker_unwired")
        # The deployment pointer stays in the message: this is a wiring fact,
        # not a transient read failure.
        self.assertIn("share-management.json", exc.message)
        self.assertIn("install-share-broker.ps1", exc.message)

    def test_configured_loader_result_is_adopted(self):
        sentinel = object()
        calls = []

        def loader():
            calls.append("load")
            return sentinel

        prod.configure_broker_loader(loader)
        self.assertIs(prod._default_broker(), sentinel)
        self.assertEqual(calls, ["load"])

    def test_loader_returning_none_is_the_same_honest_refusal(self):
        prod.configure_broker_loader(lambda: None)
        with self.assertRaises(OperationError) as caught:
            prod._default_broker()
        self.assertEqual(caught.exception.code, "share_broker_unwired")
        self.assertIn("install-share-broker.ps1",
                      caught.exception.message)

    def test_production_seams_prefers_the_injected_broker(self):
        prod.configure_broker_loader(
            mock.Mock(side_effect=AssertionError("loader must not run")))
        sentinel = object()
        seams = prod.production_seams(broker=sentinel)
        self.assertIs(seams["_BROKER_OPS"]._broker, sentinel)
        self.assertIs(seams["_MAINTENANCE_BEGIN"].keywords["broker"],
                      sentinel)

    def test_production_seams_uses_the_configured_loader(self):
        sentinel = object()
        loader = mock.Mock(return_value=sentinel)
        prod.configure_broker_loader(loader)
        seams = prod.production_seams()
        loader.assert_called_once_with()
        self.assertIs(seams["_BROKER_OPS"]._broker, sentinel)


class BackendConfigTests(unittest.TestCase):
    """The explicit backend configuration: all fields, no environment reads."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _config(self, **overrides):
        values = {
            "share_run_root": str(self.root / "run-root"),
            "share_secret_key": str(self.root / "secrets" / "share-secret.key"),
            "attribution_key": str(self.root / "run" / "attribution.key"),
            "token": str(self.root / "backend" / "token.json"),
            "service_log": str(self.root / "backend" / "backend.log"),
            "usage_log": str(self.root / "backend" / "usage.jsonl"),
            "control_token": str(self.root / "backend" / "control-token"),
            "reauth_slot": str(self.root / "backend" / "reauth.state"),
            "pid": str(self.root / "backend" / "backend.pid"),
            "backend_dir": str(self.root / "backend"),
            "profile": str(self.root / "backend-profile" / "explicit-profile"),
            "backend_port": 18110,
            "backend_base_url": "http://127.0.0.1:18111",
            # standards-config-10: ``BackendConfig`` now refuses a gateway
            # directory outside the package (the cloud root already did), so the
            # explicit fixture names the in-package ``chat2api`` source dir.
            "backend_gateway_dir": str(AI_GATEWAY / "chat2api"),
        }
        values.update(overrides)
        return prod.BackendConfig(**values)

    def test_all_fields_are_required(self):
        with self.assertRaises(TypeError):
            prod.BackendConfig(
                share_run_root=str(self.root),
                share_secret_key=str(self.root / "k"),
                attribution_key=str(self.root / "a"),
                token=str(self.root / "t"),
                service_log=str(self.root / "s"),
                usage_log=str(self.root / "u"),
                control_token=str(self.root / "c"),
                reauth_slot=str(self.root / "r"),
                pid=str(self.root / "p"),
                backend_dir=str(self.root / "b"),
                profile=str(self.root / "profile"),
                backend_port=18110,
                backend_base_url="http://127.0.0.1:18111")
        # backend_gateway_dir omitted above: never silently defaulted

    def test_relative_paths_and_bad_ports_are_refused(self):
        for overrides in ({"backend_dir": "relative/backend"},
                          {"share_secret_key": "relative.key"},
                          {"backend_port": 0},
                          {"backend_port": 70000},
                          {"backend_port": True},
                          {"backend_base_url": ""}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self._config(**overrides)

    def test_paths_and_child_env_use_only_the_config(self):
        config = self._config()
        ops = prod.production_backend_ops(config=config)
        self.assertEqual(ops._gateway_dir, config.backend_gateway_dir)
        with mock.patch.object(prod.os, "environ", _NoEnvironment()):
            paths = ops._paths()
            child_env = ops._child_env(paths)
            cmdline = ops._child_cmdline(paths)
        self.assertEqual(paths["dir"], config.backend_dir)
        self.assertEqual(paths["port"], config.backend_port)
        self.assertEqual(paths["token"], config.token)
        self.assertEqual(paths["profile"], config.profile)
        self.assertNotEqual(paths["profile"],
                            os.path.join(config.backend_dir, "edge-profile"))
        self.assertIn("--profile", cmdline)
        self.assertEqual(cmdline[cmdline.index("--profile") + 1],
                         config.profile)
        self.assertEqual(paths["log"], config.service_log)
        self.assertEqual(paths["ledger"], config.usage_log)
        self.assertEqual(paths["control_token"], config.control_token)
        self.assertEqual(paths["reauth_slot"], config.reauth_slot)
        self.assertEqual(paths["attribution_key"], config.attribution_key)
        self.assertEqual(paths["pid"], config.pid)
        self.assertEqual(paths["share_run_root"], config.share_run_root)
        self.assertEqual(paths["share_secret_key"], config.share_secret_key)
        for key, expected in (
                ("CHAT2API_TOKEN_FILE", config.token),
                ("CHAT2API_SERVICE_LOG", config.service_log),
                ("CHAT2API_USAGE_LOG", config.usage_log),
                ("CHAT2API_CONTROL_TOKEN_FILE", config.control_token),
                ("CHAT2API_REAUTH_SLOT_FILE", config.reauth_slot),
                ("CHAT2API_SHARE_SECRET_KEY_FILE",
                 config.share_secret_key),
                ("CHAT2API_SHARE_ATTRIBUTION_KEY_FILE",
                 config.attribution_key)):
            self.assertEqual(child_env.get(key), expected)
        self.assertEqual(child_env.get("CHAT2API_LAUNCH_SOURCE"), "console")
        self.assertEqual(cmdline[cmdline.index("--base-url") + 1],
                         config.backend_base_url)
        self.assertEqual(cmdline[cmdline.index("--port") + 1],
                         str(config.backend_port))
        self.assertEqual(cmdline[cmdline.index("--profile") + 1],
                         paths["profile"])

    def test_linux_backend_ops_accept_the_same_explicit_config(self):
        config = self._config()
        ops = prod._LinuxBackendOps(
            gateway_dir=config.backend_gateway_dir, config=config)
        with mock.patch.object(prod.os, "environ", _NoEnvironment()):
            paths = ops._paths()
            child_env = ops._child_env(paths)
        self.assertEqual(paths["dir"], config.backend_dir)
        self.assertEqual(paths["port"], config.backend_port)
        self.assertEqual(child_env["CHAT2API_SHARE_SECRET_KEY_FILE"],
                         config.share_secret_key)
        self.assertEqual(child_env["CHAT2API_SHARE_ATTRIBUTION_KEY_FILE"],
                         config.attribution_key)

    def test_no_config_keeps_the_historical_environment_behavior(self):
        backend_dir = str(self.root / "env-backend")
        run_root = str(self.root / "env-run-root")
        with mock.patch.dict(os.environ, {
                "CONSOLE_SHARE_BACKEND_DIR": backend_dir,
                "CONSOLE_SHARE_BACKEND_PORT": "18123",
                "CONSOLE_SHARE_BACKEND_BASE_URL": "http://127.0.0.1:18124",
                prod.SHARE_RUN_ROOT_ENV: run_root}, clear=False):
            os.environ.pop(prod.SHARE_ATTRIBUTION_KEY_FILE_ENV, None)
            ops = prod.production_backend_ops()
            paths = ops._paths()
            child_env = ops._child_env(paths)
        self.assertEqual(paths["dir"], backend_dir)
        self.assertEqual(paths["port"], 18123)
        self.assertEqual(paths["attribution_key"],
                         os.path.join(run_root, "run",
                                       prod.SHARE_ATTRIBUTION_KEY_NAME))
        self.assertEqual(child_env["CHAT2API_TOKEN_FILE"],
                         os.path.join(backend_dir, "token.json"))
        # The historical behavior does not set the POSIX secret-key redirect;
        # the child inherits whatever the parent process already had.
        self.assertNotIn("CHAT2API_SHARE_SECRET_KEY_FILE", child_env)
        self.assertEqual("CHAT2API_REAUTH_SLOT_FILE" in child_env,
                         os.name == "posix")


if __name__ == "__main__":
    unittest.main()
