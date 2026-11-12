"""S4: offline runtime broker tests — projection file and fail-closed host.

Does not install a broker, start any live share, or invoke PowerShell.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from share.management import BrokerError, ShareManager  # noqa: E402
from share.runtime import (DeployBroker, PowerShellStageExecutor,  # noqa: E402
                           ProjectionFileBroker)


class ProjectionFileBrokerTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)

    def test_publish_writes_fixed_projection_and_acks_in_test_mode(self):
        proj = Path(self.td.name) / "projection.json"
        broker = ProjectionFileBroker(proj, ack=True)
        mgr = ShareManager(store_path=Path(self.td.name) / "store.db",
                           broker=broker)
        res = mgr.create_key("friendA")
        self.assertTrue(res["in_sync"])
        self.assertTrue(proj.exists())
        data = __import__("json").loads(proj.read_text(encoding="utf-8"))
        self.assertEqual(data["config_generation"], res["desired_generation"])
        self.assertEqual(data["data"][0]["name"], "friendA")

    def test_no_ack_keeps_store_offline(self):
        proj = Path(self.td.name) / "projection.json"
        broker = ProjectionFileBroker(proj, ack=False)
        mgr = ShareManager(store_path=Path(self.td.name) / "store.db",
                           broker=broker)
        res = mgr.create_key("friendA")
        self.assertFalse(res["in_sync"])
        self.assertTrue(proj.exists())

    def test_projection_broker_cannot_control_host(self):
        broker = ProjectionFileBroker(Path(self.td.name) / "p.json")
        with self.assertRaises(BrokerError):
            broker.start()

    def test_projection_broker_refuses_generation_regression(self):
        # R5-b 运行端：一个晚到的旧代次发布不得覆盖已在线的新投影。
        import json as _json
        proj = Path(self.td.name) / "projection.json"
        broker = ProjectionFileBroker(proj, ack=True)
        mgr = ShareManager(store_path=Path(self.td.name) / "store.db",
                           broker=broker)
        mgr.create_key("friendA")
        stale = _json.loads(proj.read_text(encoding="utf-8"))
        mgr.create_key("friendB")  # 新一代已写入投影文件
        with self.assertRaises(BrokerError) as cm:
            broker.publish(stale)
        self.assertIn("regression", str(cm.exception))
        on_disk = _json.loads(proj.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["config_generation"],
                         stale["config_generation"] + 1)

    def test_projection_broker_refuses_same_generation_content_change(self):
        import json as _json
        proj = Path(self.td.name) / "projection.json"
        broker = ProjectionFileBroker(proj, ack=True)
        mgr = ShareManager(store_path=Path(self.td.name) / "store.db",
                           broker=broker)
        mgr.create_key("friendA")
        on_disk = _json.loads(proj.read_text(encoding="utf-8"))
        tampered = _json.loads(_json.dumps(on_disk))
        tampered["data"] = []
        with self.assertRaises(BrokerError) as cm:
            broker.publish(tampered)
        self.assertIn("mismatch", str(cm.exception))
        # 文件保持原发布内容，未被篡改版本覆盖。
        self.assertEqual(
            _json.loads(proj.read_text(encoding="utf-8"))["data"],
            on_disk["data"])


class DeployBrokerTests(unittest.TestCase):
    def test_uninstalled_broker_fails_closed(self):
        broker = DeployBroker()
        with self.assertRaises(BrokerError):
            broker.start()
        with self.assertRaises(BrokerError):
            broker.graceful_stop()
        with self.assertRaises(BrokerError):
            broker.emergency_stop()

    def test_executor_receives_only_fixed_stages(self):
        calls = []
        broker = DeployBroker(executor=lambda stage, projection=None:
                              calls.append((stage, projection)) or
                              {"state": "ok"},
                              projection_path="fixed-projection.json")
        broker.start()
        broker.graceful_stop()
        broker.emergency_stop()
        broker.publish(projection={"config_generation": 3})
        self.assertEqual([c[0] for c in calls],
                         ["start", "graceful_stop", "emergency_stop",
                          "publish_projection"])
        # publish must not discard the projection payload.
        self.assertEqual(calls[-1][1], {"config_generation": 3})

    def test_powershell_executor_stage_map_is_fixed_and_fail_closed(self):
        exe = PowerShellStageExecutor(Path("D:\\run-deploy-guarded.ps1"))
        self.assertEqual(exe.stage_map["start"], "Start")
        self.assertEqual(exe.stage_map["graceful_stop"], "DrainStop")
        self.assertEqual(exe.stage_map["emergency_stop"], "Stop")
        self.assertEqual(exe.stage_map["publish_projection"], "PublishProjection")
        with self.assertRaises(BrokerError):
            exe("arbitrary-stage")
        # The authorized projection writer does not exist offline; a publish
        # must fail closed rather than silently reuse Prep.
        with self.assertRaises(BrokerError):
            exe("publish_projection", projection={"config_generation": 3})


if __name__ == "__main__":
    unittest.main()
