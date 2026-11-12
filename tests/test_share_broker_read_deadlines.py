"""Read deadlines cover their OS evidence; mutation deadlines stay fixed."""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import share.broker_client as client_module


class EvidenceReadDeadlineTests(unittest.TestCase):
    def exchange(self, method, elapsed):
        now = [0.0]

        class Transport:
            def open(self, deadline=None):
                return self

            def send_all(self, data, deadline=None):
                request = json.loads(data)
                self.reply = json.dumps({"v": 2, "ok": True, "error_code": "ok",
                    "request_id": request["request_id"], "result": {"host_running": True,
                    "verdict": True, "checked_at": 1}}).encode() + b"\n"

            def recv_line(self, deadline):
                now[0] += elapsed
                if now[0] >= deadline:
                    raise TimeoutError("simulated OS evidence is still pending")
                return self.reply

            def close(self):
                pass

        client = client_module.BrokerClient(Transport, "isolated-key")
        with mock.patch.object(client_module.time, "monotonic", side_effect=lambda: now[0]):
            return method(client)

    def test_three_source_host_read_can_finish_after_short_command_deadline(self):
        result = self.exchange(lambda client: client.host_verify("running"), 14.0)
        self.assertTrue(result.ok, "three separate bounded OS reads must fit the read exchange")
        self.assertIs(result.result["host_running"], True)

    def test_security_read_has_room_for_one_command_and_reply(self):
        self.assertTrue(self.exchange(lambda client: client.host_evidence("limits"), 11.0).ok)

    def test_start_keeps_its_existing_mutation_deadline(self):
        result = self.exchange(lambda client: client.start(), 11.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "deadline_exceeded")

    def test_busy_pipe_wait_uses_remaining_exchange_budget(self):
        now = [0.0]

        class Kernel:
            def CreateFileW(self, *args):
                return 41 if now[0] >= 3.0 else 0

            def GetLastError(self):
                return 231

            def CloseHandle(self, handle):
                return 1

        transport = client_module.NamedPipeBrokerTransport(kernel=Kernel())
        with mock.patch.object(client_module.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(client_module.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            transport.open(deadline=5.0)
            transport.close()
        self.assertGreaterEqual(now[0], 3.0)
        self.assertLess(now[0], 5.0)

    def test_absent_pipe_still_fails_at_short_installation_bound(self):
        now = [0.0]

        class Kernel:
            def CreateFileW(self, *args):
                return 0

            def GetLastError(self):
                return 2

        transport = client_module.NamedPipeBrokerTransport(kernel=Kernel())
        with mock.patch.object(client_module.time, "monotonic", side_effect=lambda: now[0]), \
             mock.patch.object(client_module.time, "sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
            with self.assertRaises(OSError):
                transport.open(deadline=35.0)
        self.assertLessEqual(now[0], 2.1)


if __name__ == "__main__":
    unittest.main()
