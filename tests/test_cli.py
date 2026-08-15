"""End-to-end CLI behaviour, including the exit codes hooks depend on."""

from __future__ import annotations

import json
import os
import subprocess
import unittest

from support import KIT, KitTestCase

PQA = str(KIT / "bin" / "pqa")


def run(*args, home=None, cwd=None):
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    if home:
        env["PQA_HOME"] = str(home)
    return subprocess.run(
        [PQA, *args], capture_output=True, text=True, env=env, cwd=cwd or str(KIT)
    )


class TestExitCodes(KitTestCase):
    """0 passed, 1 could not run, 2 constraint violated.

    Collapsing 1 and 2 would make a broken gate indistinguishable from a passing
    one, which is the failure a gate must not have.
    """

    def test_clean_gate_exits_zero(self):
        result = run("gate", str(self.run_path), home=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_gate_exits_two(self):
        (self.run_path / "findings.reviewed.json").unlink()
        result = run("gate", str(self.run_path), home=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("findings.reviewed.json", result.stdout)

    def test_unusable_path_exits_one(self):
        result = run("gate", str(self.root / "no-such-run"), home=self.root)
        self.assertEqual(result.returncode, 1)

    def test_self_check_exits_zero(self):
        result = run("validate", "--self-check", home=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_drift_exits_two_on_breach(self):
        path = self.run_path / "session.jsonl"
        path.write_text(path.read_text().replace("pale grey", "a race condition"))
        result = run("drift", str(self.run_path), home=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("race condition", result.stdout)


class TestSealOrdering(KitTestCase):
    """DESIGN.md section 3: conditions are fixed before execution, not after."""

    def test_seal_refuses_once_a_session_exists(self):
        result = run("seal", str(self.run_path), home=self.root)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot be sealed", result.stderr)

    def test_seal_succeeds_before_the_session(self):
        (self.run_path / "session.jsonl").unlink()
        (self.run_path / "run.lock").unlink()
        result = run("seal", str(self.run_path), home=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.run_path / "run.lock").exists())


class TestNewRun(KitTestCase):
    def test_creates_a_sealable_manifest(self):
        out = self.root / "runs" / "2026-09-01-fresh"
        result = run(
            "new-run", "--persona", "cautious-first-timer",
            "--target", "fresh-app", "--build", "deadbee",
            "--budget", "25", "--run-id", "2026-09-01-fresh",
            "--out", str(out), home=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        manifest = json.loads((out / "run.json").read_text())
        self.assertEqual(manifest["created_by"], "qa-orchestrator")
        self.assertEqual(manifest["stopping_conditions"]["step_budget"], 25)
        self.assertEqual(manifest["persona"]["id"], "cautious-first-timer")

        # Success criteria come from the card, not from the caller.
        self.assertTrue(manifest["stopping_conditions"]["goal_satisfaction"]["success_criteria"])
        self.assertTrue(manifest["stopping_conditions"]["scope_bounds"]["out_of_scope"])

        self.assertEqual(run("seal", str(out), home=self.root).returncode, 0)

    def test_refuses_to_overwrite_an_existing_run(self):
        result = run(
            "new-run", "--persona", "time-starved-parent",
            "--target", "notes-app", "--build", "x",
            "--run-id", "2026-08-15-notes-app", "--out", str(self.run_path),
            home=self.root,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("not overwritten", result.stderr)

    def test_unknown_persona_fails(self):
        result = run(
            "new-run", "--persona", "nobody", "--target", "x", "--build", "y",
            home=self.root,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no persona card", result.stderr)


class TestOtherCommands(unittest.TestCase):
    def test_personas_lists_the_cards(self):
        result = run("personas")
        self.assertEqual(result.returncode, 0)
        self.assertIn("time-starved-parent", result.stdout)

    def test_fingerprint_is_deterministic(self):
        args = ("fingerprint", "--surface", "a/b", "--action", "tap", "--symptom", "crash")
        self.assertEqual(run(*args).stdout, run(*args).stdout)

    def test_inject_emits_a_valid_record(self):
        result = run("inject", "--persona", "time-starved-parent", "--record", "--step", "8")
        self.assertEqual(result.returncode, 0)
        record = json.loads(result.stdout)
        self.assertEqual(record["kind"], "persona_injection")
        self.assertEqual(record["step"], 8)

    def test_inject_prose_avoids_the_forbidden_vocabulary(self):
        # The re-injected block is itself session context; if it contained a
        # never_uses term it would trip the tripwire it exists to prevent.
        from pqa import drift, personas

        card = personas.load("time-starved-parent")
        block = personas.summary_block(card)
        body = block.split("You would never say:")[0]
        self.assertEqual(drift.scan([{"step": 0, "felt": body}], card.never_uses), [])

    def test_signals_on_the_example_directory(self):
        result = run("signals", str(KIT / "examples" / "runs"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("2026-08-15-notes-app", result.stdout)


if __name__ == "__main__":
    unittest.main()
