"""The platform layer: gate stages, hook envelopes, the guard, and the adapters.

Same method as test_gate.py: start from a run known to pass, break one thing,
and assert that every platform's envelope refuses it in that platform's terms.
A Codex block that Claude Code would read as success, or an Antigravity deny
that the IDE would ignore, is a gate that only works on one platform.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path

from support import KIT, KitTestCase

PQA = str(KIT / "bin" / "pqa")


def hook(args, payload=None, run_dir=None, env_extra=None, script=None):
    env = dict(os.environ, NO_COLOR="1")
    env.pop("PQA_ROLE", None)
    env.pop("PQA_STAGE", None)
    if run_dir is not None:
        env["PQA_RUN_DIR"] = str(run_dir)
    env.update(env_extra or {})
    command = [str(script), *args] if script else [PQA, *args]
    return subprocess.run(
        command, input=json.dumps(payload) if payload is not None else "",
        capture_output=True, text=True, env=env, cwd=str(KIT),
    )


class TestStages(KitTestCase):
    """Each stage demands what its role must have produced, and no more."""

    def stage(self, name):
        from pqa.gate import run_gate

        return run_gate(self.run_dir(), name)

    def test_every_stage_passes_on_a_complete_run(self):
        for name in ("session", "candidates", "review", "evaluation"):
            self.assertTrue(self.stage(name).ok, name)

    def test_session_stage_does_not_need_downstream_artifacts(self):
        for name in ("findings.candidates.json", "findings.reviewed.json", "evaluation.json"):
            (self.run_path / name).unlink()
        self.assertTrue(self.stage("session").ok)
        self.assertFalse(self.stage("candidates").ok)

    def test_session_stage_still_checks_the_session(self):
        path = self.run_path / "session.jsonl"
        path.write_text(path.read_text().replace("pale grey", "a race condition"))
        self.assertFalse(self.stage("session").ok)

    def test_candidates_stage_does_not_need_the_review(self):
        (self.run_path / "findings.reviewed.json").unlink()
        self.assertTrue(self.stage("candidates").ok)
        self.assertFalse(self.stage("review").ok)

    def test_evaluation_stage_needs_the_evaluation(self):
        (self.run_path / "evaluation.json").unlink()
        self.assertTrue(self.stage("review").ok)
        report = self.stage("evaluation")
        self.assertFalse(report.ok)
        self.assertIn("evaluation.json", report.render("x"))

    def test_evaluation_must_score_the_sealed_persona(self):
        data = self.read("evaluation.json")
        data["persona"]["version"] = "9.9.9"
        self.write("evaluation.json", data)
        self.assertFalse(self.stage("evaluation").ok)

    def test_default_is_the_historical_full_gate(self):
        from pqa.gate import run_gate

        (self.run_path / "evaluation.json").unlink()
        self.assertTrue(run_gate(self.run_dir()).ok)

    def test_unknown_stage_is_a_usage_error(self):
        from pqa.core import PqaError

        with self.assertRaises(PqaError):
            self.stage("everything")


class TestRoles(unittest.TestCase):
    def test_codex_and_claude_names_resolve_to_the_same_role(self):
        from pqa import roles

        self.assertIs(roles.lookup("user_agent"), roles.lookup("user-agent"))
        self.assertEqual(roles.stage_for("qa_reviewer"), "review")

    def test_unknown_agents_get_the_full_gate(self):
        from pqa import roles

        self.assertEqual(roles.stage_for("Explore"), roles.DEFAULT_STAGE)
        self.assertEqual(roles.stage_for(None), roles.DEFAULT_STAGE)

    def test_every_role_has_a_definition(self):
        from pqa import roles

        for name in roles.ROLES:
            definition = roles.load_definition(name)
            self.assertEqual(definition.name, name)
            self.assertTrue(definition.body.strip())


class TestGateEnvelopes(KitTestCase):
    def break_review(self):
        (self.run_path / "findings.reviewed.json").unlink()

    def test_claude_blocks_with_exit_two_and_stderr(self):
        self.break_review()
        result = hook(["gate", "--format", "claude"], {"hook_event_name": "Stop"}, self.run_path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("findings.reviewed.json", result.stderr)
        self.assertIn("Do not summarize the run as complete", result.stderr)

    def test_claude_passes_with_the_report_on_stdout(self):
        result = hook(["gate", "--format", "claude"], {"hook_event_name": "Stop"}, self.run_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("review gate", result.stdout)

    def test_codex_blocks_with_a_json_decision(self):
        self.break_review()
        result = hook(["gate", "--format", "codex"], {"hook_event_name": "Stop"}, self.run_path)
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("findings.reviewed.json", out["reason"])

    def test_codex_pass_prints_nothing(self):
        # Plain text on stdout is invalid for a Codex Stop hook.
        result = hook(["gate", "--format", "codex"], {"hook_event_name": "Stop"}, self.run_path)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_agy_stop_continues(self):
        self.break_review()
        result = hook(["gate", "--format", "agy"], {"executionNum": 1}, self.run_path)
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout)
        self.assertEqual(out["decision"], "continue")
        self.assertTrue(out["reason"])

    def test_agy_pass_is_an_empty_object(self):
        result = hook(["gate", "--format", "agy"], {}, self.run_path)
        self.assertEqual(json.loads(result.stdout), {})

    def test_subagent_stop_is_gated_at_the_role_stage(self):
        self.break_review()
        for fmt in ("claude", "codex"):
            payload = {"hook_event_name": "SubagentStop", "agent_type": "user_agent"}
            result = hook(["gate", "--format", fmt], payload, self.run_path)
            self.assertEqual(result.returncode, 0, fmt + result.stderr)
            self.assertNotIn('"block"', result.stdout)

            payload["agent_type"] = "qa-reviewer"
            result = hook(["gate", "--format", fmt], payload, self.run_path)
            blocked = result.returncode == 2 or '"block"' in result.stdout
            self.assertTrue(blocked, fmt)

    def test_role_from_the_environment_when_the_payload_has_none(self):
        self.break_review()
        result = hook(["gate", "--format", "agy"], {}, self.run_path, {"PQA_ROLE": "qa-agent"})
        self.assertEqual(json.loads(result.stdout), {})

    def test_no_run_in_progress_passes(self):
        empty = self.root / "empty-runs"
        empty.mkdir()
        env = {"PQA_RUNS_DIR": str(empty)}
        for fmt, expected in (("claude", ""), ("codex", ""), ("agy", "{}")):
            result = hook(["gate", "--format", fmt], {}, None, env)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), fmt)

    def test_newest_run_is_located_without_being_told(self):
        self.break_review()
        result = hook(["gate", "--format", "codex"], {}, None,
                      {"PQA_RUNS_DIR": str(self.root / "runs"), "PQA_HOME": str(self.root)})
        self.assertEqual(json.loads(result.stdout)["decision"], "block")


class TestDriftEnvelopes(KitTestCase):
    def drop_injection(self):
        # Cut the example session back to its step-8 observation, without the
        # persona_injection that followed it, so one is due on the next tick.
        path = self.run_path / "session.jsonl"
        kept = []
        for line in path.read_text().splitlines():
            record = json.loads(line)
            if record["kind"] == "observation" and record["step"] <= 8:
                kept.append(line)
        path.write_text("\n".join(kept) + "\n")

    def test_claude_envelope_is_unchanged(self):
        self.drop_injection()
        result = hook(["drift-tick", str(self.run_path)], None)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("You are Priya", out["hookSpecificOutput"]["additionalContext"])

    def test_agy_injects_an_ephemeral_message(self):
        self.drop_injection()
        result = hook(["drift-tick", str(self.run_path), "--format", "agy"], {"invocationNum": 3})
        out = json.loads(result.stdout)
        self.assertIn("You are Priya", out["injectSteps"][0]["ephemeralMessage"])
        self.assertIn("persona_injection", (self.run_path / "session.jsonl").read_text())

    def test_injection_is_recorded_once(self):
        self.drop_injection()
        hook(["drift-tick", str(self.run_path), "--format", "codex"], {})
        again = hook(["drift-tick", str(self.run_path), "--format", "codex"], {})
        self.assertEqual(again.stdout, "")

    def test_breach_in_each_envelope(self):
        path = self.run_path / "session.jsonl"
        path.write_text(path.read_text().replace("pale grey", "a race condition"))

        claude = hook(["drift-tick", str(self.run_path), "--format", "claude"], None)
        self.assertEqual(claude.returncode, 2)
        self.assertIn("race condition", claude.stderr)

        codex = hook(["drift-tick", str(self.run_path), "--format", "codex"], {})
        self.assertEqual(json.loads(codex.stdout)["decision"], "block")

        agy = hook(["drift-tick", str(self.run_path), "--format", "agy"], {})
        self.assertIn("race condition", json.loads(agy.stdout)["injectSteps"][0]["ephemeralMessage"])


class TestGuard(KitTestCase):
    def guard(self, payload, fmt="codex", role=None):
        env = {"PQA_ROLE": role} if role else {}
        payload.setdefault("cwd", str(self.root))
        result = hook(["guard", "--format", fmt], payload, self.run_path, env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None
        out = json.loads(result.stdout)
        if fmt == "agy":
            return out.get("reason") if out.get("decision") == "deny" else None
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def rel(self, name):
        return f"runs/{self.run_path.name}/{name}"

    def patch(self, name, added):
        return {"tool_name": "apply_patch", "tool_input": {"command": (
            f"*** Begin Patch\n*** Update File: {self.rel(name)}\n@@\n+{added}\n*** End Patch\n")}}

    def test_session_log_is_frozen_once_candidates_exist(self):
        reason = self.guard(self.patch("session.jsonl", '{"kind":"observation"}'))
        self.assertIn("frozen", reason)

    def test_session_log_may_be_written_mid_session(self):
        for name in ("findings.candidates.json", "findings.reviewed.json", "evaluation.json"):
            (self.run_path / name).unlink()
        self.assertIsNone(self.guard(self.patch("session.jsonl", '{"felt":"it did not save"}')))

    def test_never_uses_is_refused_before_it_is_written(self):
        (self.run_path / "findings.candidates.json").unlink()
        record = json.dumps({"kind": "observation", "step": 11, "felt": "probably a race condition"})
        reason = self.guard(self.patch("session.jsonl", record))
        self.assertIn("race condition", reason)

    def test_shell_append_with_never_uses_is_refused(self):
        (self.run_path / "findings.candidates.json").unlink()
        command = f"echo '{{\"felt\":\"the backend broke\"}}' >> {self.rel('session.jsonl')}"
        payload = {"toolCall": {"name": "run_command", "args": {"CommandLine": command}},
                   "workspacePaths": [str(self.root)]}
        self.assertIn("backend", self.guard(payload, fmt="agy"))

    def test_sealed_manifest_cannot_be_edited(self):
        payload = {"tool_name": "Write", "tool_input": {
            "file_path": str(self.run_path / "run.json"), "content": "{}"}}
        self.assertIn("sealed", self.guard(payload, fmt="claude"))

    def test_lock_is_written_by_seal_only(self):
        payload = {"toolCall": {"name": "create_file", "args": {
            "TargetFile": str(self.run_path / "run.lock"), "CodeContent": "{}"}}}
        self.assertIn("pqa seal", self.guard(payload, fmt="agy"))

    def test_judging_role_may_write_only_its_output(self):
        payload = self.patch("evaluation.json", "{}")
        self.assertIn("may write only", self.guard(payload, role="qa_reviewer"))
        payload = self.patch("findings.reviewed.json", "{}")
        self.assertIsNone(self.guard(payload, role="qa_reviewer"))

    def test_judging_role_may_not_edit_the_kit(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"command":
                   "*** Begin Patch\n*** Update File: schemas/finding.schema.json\n+x\n*** End Patch"}}
        self.assertIn("may not change", self.guard(payload, role="qa-agent"))

    def test_judging_role_has_a_read_only_shell(self):
        def shell(command):
            return self.guard({"tool_name": "Bash", "tool_input": {"command": command}},
                              role="qa-reviewer")

        self.assertIsNone(shell(f"cat {self.rel('session.jsonl')} | grep observed"))
        self.assertIsNone(shell("./bin/pqa fingerprint --surface a --action tap --symptom crash"))
        self.assertIsNone(shell(f"./bin/pqa digest {self.rel('findings.candidates.json')}"))
        self.assertIsNotNone(shell("rm -rf runs"))
        self.assertIsNotNone(shell("echo hi > notes.txt"))
        self.assertIsNotNone(shell("./bin/pqa seal runs/x"))
        self.assertIsNotNone(shell("sed -i '' s/a/b/ x"))
        self.assertIsNotNone(shell("cat $(echo x)"))

    def test_unrelated_calls_pass(self):
        self.assertIsNone(self.guard({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertIsNone(self.guard({"toolCall": {"name": "view_file", "args": {
            "AbsolutePath": str(self.run_path / "session.jsonl")}}}, fmt="agy"))

    def test_agy_no_objection_is_not_an_explicit_allow(self):
        result = hook(["guard", "--format", "agy"], {"toolCall": {"name": "view_file"}}, self.run_path)
        self.assertEqual(json.loads(result.stdout), {})


class TestRecord(KitTestCase):
    def setUp(self):
        super().setUp()
        for name in ("findings.candidates.json", "findings.reviewed.json", "evaluation.json"):
            (self.run_path / name).unlink()
        path = self.run_path / "session.jsonl"
        lines = [l for l in path.read_text().splitlines()
                 if '"terminated"' not in l and '"blocked"' not in l]
        path.write_text("\n".join(lines) + "\n")

    def record(self, entry):
        from pqa import live

        return live.record(self.run_dir(), json.dumps(entry))

    def observation(self, step, felt="I pressed it and waited"):
        return {"kind": "observation", "step": step,
                "action": {"class": "tap", "target": "the green button"},
                "expected": "my note would be there", "observed": "the page looked the same",
                "felt": felt}

    def test_valid_record_is_appended(self):
        ok, message = self.record(self.observation(11))
        self.assertTrue(ok, message)
        self.assertIn('"step": 11', (self.run_path / "session.jsonl").read_text())

    def test_vocabulary_breach_is_refused_and_not_written(self):
        before = (self.run_path / "session.jsonl").read_text()
        ok, message = self.record(self.observation(11, "looks like a race condition"))
        self.assertFalse(ok)
        self.assertIn("race condition", message)
        self.assertEqual((self.run_path / "session.jsonl").read_text(), before)

    def test_verdict_fields_are_refused(self):
        entry = self.observation(11)
        entry["severity"] = "major"
        self.assertFalse(self.record(entry)[0])

    def test_duplicate_step_is_refused(self):
        self.assertFalse(self.record(self.observation(3))[0])

    def test_injection_rides_back_in_the_result(self):
        path = self.run_path / "session.jsonl"
        kept = [l for l in path.read_text().splitlines() if json.loads(l)["step"] < 8]
        path.write_text("\n".join(kept) + "\n")
        ok, message = self.record(self.observation(8))
        self.assertTrue(ok, message)
        self.assertIn("You are Priya", message)

    def test_nothing_follows_termination(self):
        ok, _ = self.record({"kind": "terminated", "step": 11, "reason": "goal_met"})
        self.assertTrue(ok)
        self.assertFalse(self.record(self.observation(12))[0])

    def test_cli_exit_codes(self):
        result = subprocess.run(
            [PQA, "record", str(self.run_path), json.dumps(self.observation(11, "the API died"))],
            capture_output=True, text=True, env=dict(os.environ, PQA_HOME=str(self.root)))
        self.assertEqual(result.returncode, 2)


class TestShims(KitTestCase):
    """The shims fail closed: if pqa cannot run, each platform is told to block."""

    def test_claude_shim_blocks_a_failing_run(self):
        (self.run_path / "findings.reviewed.json").unlink()
        result = hook(["gate"], {"hook_event_name": "Stop"}, self.run_path,
                      script=KIT / "adapters" / "claude" / "hook.sh")
        self.assertEqual(result.returncode, 2)

    def test_shims_fail_closed_when_pqa_breaks(self):
        broken = {"PQA_RUN_DIR": str(self.root / "not-a-dir"), "PQA_HOME": str(self.root)}
        claude = hook(["gate"], {}, None, broken, script=KIT / "adapters" / "claude" / "hook.sh")
        self.assertEqual(claude.returncode, 2)
        # A guard that cannot locate its run has nothing to protect and says
        # nothing; the gate, by contrast, must refuse.
        codex = hook(["guard"], {}, None, broken, script=KIT / "adapters" / "codex" / "hook.sh")
        self.assertEqual((codex.returncode, codex.stdout), (0, ""))
        gate = hook(["gate"], {}, None, broken, script=KIT / "adapters" / "codex" / "hook.sh")
        self.assertEqual(json.loads(gate.stdout)["decision"], "block")
        agy = hook(["gate"], {}, None, broken, script=KIT / "adapters" / "antigravity" / "hook.sh")
        self.assertEqual(json.loads(agy.stdout)["decision"], "continue")


class TestAdapterFiles(unittest.TestCase):
    def test_codex_agent_files_are_generated_from_the_claude_definitions(self):
        result = subprocess.run(
            ["python3", str(KIT / "adapters" / "codex" / "generate.py"), "--check"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hook_configs_point_at_existing_shims(self):
        configs = {
            KIT / ".claude" / "settings.json": "adapters/claude/hook.sh",
            KIT / ".codex" / "hooks.json": "adapters/codex/hook.sh",
            KIT / ".agents" / "hooks.json": "adapters/antigravity/hook.sh",
        }
        for path, shim in configs.items():
            text = path.read_text()
            json.loads(text)
            self.assertIn(shim, text, path)
            self.assertTrue((KIT / shim).exists())
            self.assertTrue(os.access(KIT / shim, os.X_OK), shim)

    def test_antigravity_runner_reports_a_missing_sdk(self):
        try:
            import google.antigravity  # noqa: F401
            self.skipTest("SDK installed")
        except ImportError:
            pass
        result = subprocess.run(
            ["python3", str(KIT / "adapters" / "antigravity" / "run_pipeline.py"),
             "--persona", "x", "--target", "y", "--build", "z"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pip install", result.stderr)


if __name__ == "__main__":
    unittest.main()
