"""The hooks, driven with the JSON Claude Code sends them.

The gate tests prove each check fires on a finished run. These prove the hooks
fire at the right moment in an unfinished one -- which is where the earlier
wiring broke: a gate that demanded the review when user-agent stopped could
never let user-agent stop, and a drift tripwire that asked for the offending
record to be rewritten erased the evidence it existed to collect.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import unittest

from support import KIT, KitTestCase


def payload(agent=None, event="SubagentStop"):
    data = {"hook_event_name": event, "stop_hook_active": False}
    if agent:
        data["agent_type"] = agent
    return json.dumps(data)


class HookTestCase(KitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved_env = {k: os.environ.pop(k, None) for k in ("PQA_RUN_DIR", "PQA_RUNS_DIR")}
        from pqa.core import set_active

        set_active(self.run_path)

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is not None:
                os.environ[key] = value
        super().tearDown()

    def gate_hook(self, agent=None, event="SubagentStop"):
        from pqa.hook import gate_hook

        return gate_hook(payload(agent, event))

    def strip_to(self, *keep):
        """Reduce the example to the artifacts that exist at some mid-pipeline point."""
        for name in ("findings.candidates.json", "findings.reviewed.json", "evaluation.json"):
            if name not in keep:
                (self.run_path / name).unlink()


# --------------------------------------------------------------------------
# the gate, stage by stage
# --------------------------------------------------------------------------

class TestGateIsStaged(HookTestCase):
    def test_user_agent_can_stop_before_anything_downstream_exists(self):
        # The original defect: this exited 2 because the candidates were missing.
        self.strip_to()
        result = self.gate_hook("user-agent")
        self.assertEqual(result.code, 0, result.stderr)

    def test_user_agent_is_still_held_to_the_session_log(self):
        self.strip_to()
        path = self.run_path / "session.jsonl"
        lines = [l for l in path.read_text().splitlines() if '"terminated"' not in l]
        path.write_text("\n".join(lines) + "\n")
        (self.run_path / "session.ledger.jsonl").unlink()
        result = self.gate_hook("user-agent")
        self.assertEqual(result.code, 2)
        self.assertIn("terminated", result.stderr)

    def test_qa_agent_must_leave_candidates(self):
        self.strip_to()
        result = self.gate_hook("qa-agent")
        self.assertEqual(result.code, 2)
        self.assertIn("findings.candidates.json", result.stderr)

    def test_qa_agent_is_not_held_to_the_review(self):
        self.strip_to("findings.candidates.json")
        self.assertEqual(self.gate_hook("qa-agent").code, 0)

    def test_reviewer_must_leave_a_review(self):
        self.strip_to("findings.candidates.json")
        result = self.gate_hook("qa-reviewer")
        self.assertEqual(result.code, 2)
        self.assertIn("findings.reviewed.json", result.stderr)

    def test_reviewer_is_not_held_to_the_evaluation(self):
        self.strip_to("findings.candidates.json", "findings.reviewed.json")
        self.assertEqual(self.gate_hook("qa-reviewer").code, 0)

    def test_final_stop_requires_the_evaluation(self):
        self.strip_to("findings.candidates.json", "findings.reviewed.json")
        result = self.gate_hook(event="Stop")
        self.assertEqual(result.code, 2)
        self.assertIn("evaluation.json", result.stderr)

    def test_main_stop_cannot_skip_the_review(self):
        # Staging must not reopen the hole the gate exists for: a pipeline that
        # never dispatched qa-reviewer still cannot finish.
        self.strip_to("findings.candidates.json")
        result = self.gate_hook(event="Stop")
        self.assertEqual(result.code, 2)
        self.assertIn("findings.reviewed.json", result.stderr)

    def test_plugin_scoped_agent_names_resolve(self):
        self.strip_to()
        self.assertEqual(self.gate_hook("persona-qa-kit:user-agent").code, 0)

    def test_unrelated_subagent_is_not_gated(self):
        self.strip_to()
        self.assertEqual(self.gate_hook("Explore").code, 0)

    def test_without_agent_type_gates_what_exists(self):
        self.strip_to()
        self.assertEqual(self.gate_hook(None, "SubagentStop").code, 0)

    def test_sealed_but_unstarted_run_does_not_block_the_main_session(self):
        self.strip_to()
        (self.run_path / "session.jsonl").unlink()
        self.assertEqual(self.gate_hook(event="Stop").code, 0)

    def test_no_active_run_passes(self):
        from pqa.core import active_pointer

        active_pointer().unlink()
        self.strip_to()
        self.assertEqual(self.gate_hook("qa-agent").code, 0)

    def test_final_pass_clears_the_active_run(self):
        from pqa.core import active_run

        self.assertEqual(self.gate_hook(event="Stop").code, 0)
        self.assertIsNone(active_run())


class TestGateCannotLoopForever(HookTestCase):
    def test_gives_up_blocking_but_never_passes_the_run(self):
        from pqa.gate import run_gate

        self.strip_to()
        os.environ["PQA_GATE_MAX_BLOCKS"] = "2"
        try:
            codes = [self.gate_hook("qa-agent").code for _ in range(3)]
            last = self.gate_hook("qa-agent")
        finally:
            del os.environ["PQA_GATE_MAX_BLOCKS"]
        self.assertEqual(codes, [2, 2, 0])
        self.assertIn("NOT valid", last.stdout)
        self.assertFalse(run_gate(self.run_dir(), "candidates").ok)

    def test_a_pass_resets_the_count(self):
        self.strip_to()
        self.assertEqual(self.gate_hook("qa-agent").code, 2)
        self.assertEqual(self.gate_hook("user-agent").code, 0)
        counts = json.loads((self.run_path / ".gate-blocks.json").read_text())
        self.assertEqual(counts, {"candidates": 1, "session": 0})


class TestShellWiring(HookTestCase):
    """The scripts settings.json actually calls, end to end."""

    def run_script(self, name, stdin):
        env = dict(os.environ, PQA_HOME=str(self.root), NO_COLOR="1")
        return subprocess.run(
            [str(KIT / "hooks" / name)], input=stdin, capture_output=True, text=True, env=env,
        )

    def test_gate_script_lets_user_agent_stop(self):
        self.strip_to()
        result = self.run_script("qa-review-gate.sh", payload("user-agent"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gate_script_blocks_with_exit_two(self):
        self.strip_to()
        result = self.run_script("qa-review-gate.sh", payload("qa-agent"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("findings.candidates.json", result.stderr)

    def test_drift_script_is_quiet_on_a_clean_log(self):
        result = self.run_script("persona-drift-check.sh", "{}")
        self.assertEqual(result.returncode, 0, result.stderr)


# --------------------------------------------------------------------------
# drift: append-only, and only new records count
# --------------------------------------------------------------------------

class TestDriftIsAppendOnly(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.strip_to()
        self.session = self.run_path / "session.jsonl"
        self.original = self.session.read_text().splitlines()
        self.session.write_text("\n".join(self.original[:7]) + "\n")  # steps 1-7
        (self.run_path / "session.ledger.jsonl").unlink()

    def tick(self):
        from pqa.hook import drift_hook

        return drift_hook("{}")

    def append(self, record):
        with self.session.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def observation(self, step, felt):
        return {
            "kind": "observation", "step": step, "ts": "2026-08-15T09:16:58Z",
            "action": {"class": "tap", "target": "the Done word", "surface": "notes/editor"},
            "expected": "it would save", "observed": "nothing happened", "felt": felt,
        }

    def records(self):
        return [json.loads(l) for l in self.session.read_text().splitlines() if l.strip()]

    def test_breach_is_recorded_not_rewritten(self):
        self.assertEqual(self.tick().code, 0)
        self.append(self.observation(8, "probably the backend is slow"))
        result = self.tick()

        self.assertEqual(result.code, 2)
        self.assertNotIn("Rewrite", result.stderr)
        self.assertIn("persona_drift", result.stderr)
        breaches = [r for r in self.records() if r["kind"] == "drift_breach"]
        self.assertEqual([(b["step"], b["term"]) for b in breaches], [(8, "backend")])
        # the offending record is still there, word for word
        self.assertIn("probably the backend is slow", self.session.read_text())

    def test_an_old_breach_does_not_fire_at_the_next_agent(self):
        self.tick()
        self.append(self.observation(8, "probably the backend is slow"))
        self.assertEqual(self.tick().code, 2)
        # qa-agent, say, now runs a tool. Nothing new in the log; nothing to say.
        self.assertEqual(self.tick().code, 0)

    def test_editing_an_earlier_record_is_caught_live_and_by_the_gate(self):
        from pqa.gate import run_gate

        self.append(self.observation(8, "probably the backend is slow"))
        self.tick()
        self.session.write_text(
            self.session.read_text().replace("probably the backend is slow", "it did not save")
        )
        result = self.tick()
        self.assertEqual(result.code, 2)
        self.assertIn("edited, not appended", result.stderr)

        self.append({"kind": "terminated", "step": 8, "ts": "2026-08-15T09:17:20Z",
                     "reason": "persona_drift"})
        self.tick()
        self.assertBlocks(run_gate(self.run_dir(), "session"), "changed after they were written")

    def test_deleting_records_is_caught_by_the_gate(self):
        from pqa.gate import run_gate

        self.tick()
        self.session.write_text("\n".join(self.original[:5]) + "\n")
        self.append({"kind": "terminated", "step": 5, "ts": "2026-08-15T09:17:20Z",
                     "reason": "goal_unreachable"})
        self.assertBlocks(run_gate(self.run_dir(), "session"), "records were deleted")

    def test_a_breach_handled_by_the_protocol_leaves_a_usable_session(self):
        from pqa.gate import run_gate

        self.tick()
        self.append(self.observation(8, "probably the backend is slow"))
        self.tick()
        self.append({"kind": "terminated", "step": 8, "ts": "2026-08-15T09:17:20Z",
                     "reason": "persona_drift"})
        self.tick()
        report = run_gate(self.run_dir(), "session")
        self.assertPasses(report)
        self.assertTrue(any("stopped there as required" in w.message for w in report.warnings))

    def test_carrying_on_after_a_breach_blocks(self):
        from pqa.gate import run_gate

        self.tick()
        self.append(self.observation(8, "probably the backend is slow"))
        self.tick()
        self.append(self.observation(9, "trying again"))
        self.append({"kind": "terminated", "step": 9, "ts": "2026-08-15T09:17:20Z",
                     "reason": "persona_drift"})
        self.tick()
        self.assertBlocks(run_gate(self.run_dir(), "session"), "vocabulary breach")

    def test_reinjection_fires_when_one_write_crosses_the_boundary(self):
        # Steps 7 -> 9 in a single write used to skip the injection due at 8.
        self.tick()
        self.append(self.observation(8, "it did not save"))
        self.append(self.observation(9, "it did not save again"))
        result = self.tick()
        self.assertEqual(result.code, 0)
        self.assertIn("additionalContext", result.stdout)
        injections = [r["step"] for r in self.records() if r["kind"] == "persona_injection"]
        self.assertEqual(injections, [9])


class TestDriftedEvidence(KitTestCase):
    """Findings may not rest on steps the persona was no longer present for."""

    def drift_at_step_nine(self):
        path = self.run_path / "session.jsonl"
        text = path.read_text().replace(
            "I have no idea if any of this is saved",
            "the backend probably threw an error here",
        ).replace('"reason":"blocked"', '"reason":"persona_drift"')
        path.write_text(text)
        (self.run_path / "session.ledger.jsonl").unlink()
        self.redigest()

    def test_accepted_finding_citing_the_drifted_step_blocks(self):
        self.drift_at_step_nine()
        self.assertBlocks(self.gate(), "at or after the vocabulary breach")

    def test_persona_drift_without_a_breach_blocks(self):
        path = self.run_path / "session.jsonl"
        path.write_text(path.read_text().replace('"reason":"blocked"', '"reason":"persona_drift"'))
        self.redigest()
        self.assertBlocks(self.gate(), "has no vocabulary breach")


class TestActiveRun(KitTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._saved = {k: os.environ.pop(k, None) for k in ("PQA_RUN_DIR", "PQA_RUNS_DIR")}

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is not None:
                os.environ[key] = value
        super().tearDown()

    def test_seal_marks_the_run_active(self):
        from pqa.cli import main
        from pqa.core import active_run

        (self.run_path / "session.jsonl").unlink()
        (self.run_path / "run.lock").unlink()
        self.assertIsNone(active_run())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["seal", str(self.run_path)]), 0)
        self.assertEqual(active_run(), self.run_path.resolve())

    def test_explicit_run_dir_wins(self):
        from pqa.core import active_run, set_active

        set_active(self.run_path)
        os.environ["PQA_RUN_DIR"] = str(self.root / "elsewhere")
        try:
            self.assertEqual(active_run(), (self.root / "elsewhere").resolve())
        finally:
            del os.environ["PQA_RUN_DIR"]

    def test_newest_run_on_disk_is_no_longer_guessed(self):
        from pqa.core import active_run

        self.assertIsNone(active_run())


if __name__ == "__main__":
    unittest.main()
