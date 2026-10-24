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
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import KIT

_SPEC = importlib.util.spec_from_file_location("sim_bridge", KIT / "adapters" / "ios-sim" / "sim_bridge.py")
sim_bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sim_bridge)


class SimBridgeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runs = base / "runs"
        self.runs.mkdir()
        self.outside = base / "outside"          # stands for anything beyond the workspace
        self.outside.mkdir()
        self.bridge = sim_bridge.Bridge("SIM-UDID", "com.example.app", base / "bridge",
                                        runs_root=self.runs, capture_dir=base / "capture")
        self.calls = []
        self.bridge._run = lambda argv, timeout=60, stdin=None: self.calls.append(argv) or ""
        self.bridge._capture_png = lambda: b"PNG-BYTES"

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

    # -- files the sandbox can plant (#1 review) --------------------------------

    def victim(self, name="victim.txt"):
        path = self.outside / name
        path.write_text("untouched", encoding="utf-8")
        return path

    def test_a_planted_response_link_is_replaced_not_followed(self):
        victim = self.victim()
        os.symlink(victim, self.bridge.responses / "007.json")
        self.request("007", {"cmd": "terminate"})

        self.bridge.serve_once()

        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")
        self.assertFalse((self.bridge.responses / "007.json").is_symlink())
        self.assertTrue(self.response("007")["ok"])

    def test_a_directory_in_the_way_does_not_stop_the_loop(self):
        (self.bridge.responses / "008.json").mkdir()
        self.request("008", {"cmd": "terminate"})
        self.request("009", {"cmd": "terminate"})

        self.assertEqual(self.bridge.serve_once(), 2)

        self.assertTrue(self.response("009")["ok"])
        self.assertEqual(list(self.bridge.requests.glob("*.json")), [])

    def test_a_request_link_is_not_followed(self):
        target = self.outside / "elsewhere.json"
        target.write_text(json.dumps({"cmd": "terminate"}), encoding="utf-8")
        os.symlink(target, self.bridge.requests / "010.json")

        self.bridge.serve_once()

        self.assertFalse(self.response("010")["ok"])
        self.assertEqual(self.calls, [], "a request read through a link must not run")
        self.assertTrue(target.exists())

    def test_a_log_link_is_not_followed(self):
        victim = self.victim()
        os.symlink(victim, self.bridge.log)
        self.request("011", {"cmd": "terminate"})

        self.assertEqual(self.bridge.serve_once(), 1)

        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")
        self.assertTrue(self.response("011")["ok"])

    def test_a_request_id_must_be_a_plain_name(self):
        self.request(".hidden", {"cmd": "terminate"})

        self.bridge.serve_once()

        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.bridge.responses.iterdir()), [])
        self.assertEqual(list(self.bridge.requests.iterdir()), [])

    def serve_within(self, seconds=5):
        """serve_once in a thread, so a hang fails the test instead of stopping the suite."""
        result = {}
        worker = threading.Thread(target=lambda: result.update(n=self.bridge.serve_once()), daemon=True)
        worker.start()
        worker.join(seconds)
        self.assertFalse(worker.is_alive(), "serve_once blocked")
        return result.get("n")

    def test_a_request_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.bridge.requests / "012.json")
        self.request("013", {"cmd": "terminate"})

        self.assertEqual(self.serve_within(), 2)

        self.assertFalse(self.response("012")["ok"])
        self.assertTrue(self.response("013")["ok"])

    def test_a_log_fifo_does_not_block_the_loop(self):
        os.mkfifo(self.bridge.log)
        self.request("014", {"cmd": "terminate"})

        self.assertEqual(self.serve_within(), 1)

        self.assertTrue(self.response("014")["ok"])

    def test_a_lone_surrogate_in_args_is_logged_not_fatal(self):
        (self.bridge.requests / "015.json").write_text('{"cmd": "terminate", "args": ["\\ud800"]}',
                                                       encoding="utf-8")
        self.request("016", {"cmd": "terminate"})

        self.assertEqual(self.serve_within(), 2)

        self.assertTrue(self.response("016")["ok"])
        self.assertEqual(len(self.bridge.log.read_text(encoding="utf-8").splitlines()), 2)

    # -- screenshots stay inside the active run ---------------------------------

    def run_dir(self, name="2026-10-25-run"):
        run = self.runs / name
        run.mkdir()
        (run / "run.json").write_text("{}", encoding="utf-8")
        return run

    def screenshot(self, run, name="step-001"):
        with mock.patch.object(sim_bridge, "active_run", return_value=run):
            return self.bridge.handle({"cmd": "screenshot", "args": [name]})

    def test_a_screenshot_is_saved_in_the_run(self):
        run = self.run_dir()
        self.screenshot(run)
        self.assertEqual((run / "screenshots" / "step-001.png").read_bytes(), b"PNG-BYTES")

    def test_a_screenshots_link_is_refused(self):
        run = self.run_dir()
        os.symlink(self.outside, run / "screenshots")
        with self.assertRaises(sim_bridge.Refused):
            self.screenshot(run)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_planted_screenshot_link_is_replaced_not_followed(self):
        run = self.run_dir()
        (run / "screenshots").mkdir()
        victim = self.victim("victim.png")
        os.symlink(victim, run / "screenshots" / "step-001.png")

        self.screenshot(run)

        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")
        self.assertEqual((run / "screenshots" / "step-001.png").read_bytes(), b"PNG-BYTES")

    def test_an_active_run_outside_runs_is_refused(self):
        elsewhere = self.outside / "fake-run"
        elsewhere.mkdir()
        (elsewhere / "run.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(sim_bridge.Refused):
            self.screenshot(elsewhere)
        self.assertFalse((elsewhere / "screenshots").exists())


if __name__ == "__main__":
    unittest.main()
