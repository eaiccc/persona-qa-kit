"""The iOS Simulator bridge: requests from the sandbox are refused, never fatal.

The bridge runs outside the sandbox and executes whatever reaches its request
directory, so a malformed request has to come back as a refusal. If it raised
instead, the bridge process would stop and the request file would stay behind
to stop it again on restart. No simulator is needed: the axe and simctl calls
are replaced with a recorder.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from support import KIT

_SPEC = importlib.util.spec_from_file_location("sim_bridge", KIT / "adapters" / "ios-sim" / "sim_bridge.py")
sim_bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sim_bridge)


class SimBridgeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = sim_bridge.Bridge("SIM-UDID", "com.example.app", Path(self.tmp.name))
        self.calls = []
        self.bridge._run = lambda argv, timeout=60, stdin=None: self.calls.append(argv) or ""

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, rid, payload):
        (self.bridge.requests / f"{rid}.json").write_text(json.dumps(payload), encoding="utf-8")

    def response(self, rid):
        return json.loads((self.bridge.responses / f"{rid}.json").read_text(encoding="utf-8"))

    def test_tap_and_long_press_need_two_numbers(self):
        for cmd in ("tap", "long-press"):
            for args in ([], [5], [5, 6, 7]):
                with self.subTest(cmd=cmd, args=args):
                    with self.assertRaises(sim_bridge.Refused):
                        self.bridge.handle({"cmd": cmd, "args": args})
        self.assertEqual(self.calls, [], "nothing may reach the simulator")

    def test_tap_with_two_numbers_reaches_axe(self):
        self.assertEqual(self.bridge.handle({"cmd": "tap", "args": [10, 20]}), "tapped (10, 20)")
        self.assertEqual(self.calls, [["axe", "tap", "-x", "10", "-y", "20", "--udid", "SIM-UDID"]])

    def test_screenshot_name_is_one_plain_line(self):
        for name in ("step-1\n", "../x", "", "a/b"):
            with self.subTest(name=name):
                with self.assertRaises(sim_bridge.Refused):
                    self.bridge.handle({"cmd": "screenshot", "args": [name]})

    def test_a_request_that_raises_is_answered_and_removed(self):
        self.request("001", {"cmd": "tap", "args": [5]})
        self.request("002", {"cmd": "ui"})          # the recorder returns "", not JSON
        self.request("003", {"cmd": "terminate"})

        self.assertEqual(self.bridge.serve_once(), 3)

        self.assertFalse(self.response("001")["ok"])
        self.assertFalse(self.response("002")["ok"])
        self.assertTrue(self.response("003")["ok"])
        self.assertEqual(list(self.bridge.requests.glob("*.json")), [])
        self.assertEqual(len(self.bridge.log.read_text(encoding="utf-8").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
