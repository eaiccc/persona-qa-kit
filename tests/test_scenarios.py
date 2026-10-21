"""The scenario layer: binding, perturbations, and two-phase steps.

Same method as test_gate.py. Each test starts from the scenario example run --
which passes -- breaks one thing, and asserts the gate refuses. The run binds
note-capture-offline (offline after step 2, background after step 6) and
records every step as an intent/outcome pair.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import unittest

from support import KIT, SCENARIO_EXAMPLE, KitTestCase

PQA = str(KIT / "bin" / "pqa")


def pqa(*args, home):
    env = dict(os.environ, NO_COLOR="1", PQA_HOME=str(home))
    return subprocess.run([PQA, *args], capture_output=True, text=True, env=env, cwd=str(KIT))


class ScenarioRunCase(KitTestCase):
    example = SCENARIO_EXAMPLE

    def index_of(self, records, kind, step=None, **match):
        for i, r in enumerate(records):
            if r["kind"] == kind and (step is None or r["step"] == step) and all(
                r.get(k) == v for k, v in match.items()
            ):
                return i
        raise AssertionError(f"no {kind} record at step {step} {match}")

    def assertWarns(self, report, needle: str) -> None:
        joined = "\n".join(f"{p.where}: {p.message}" for p in report.warnings)
        self.assertIn(needle.lower(), joined.lower(), f"no warning about {needle!r}:\n{joined}")


# --------------------------------------------------------------------------
# binding
# --------------------------------------------------------------------------

class TestScenarioBinding(ScenarioRunCase):
    def test_scenario_example_passes(self):
        report = self.gate()
        self.assertPasses(report)
        self.assertEqual(report.warnings, [])

    def test_editing_the_scenario_after_the_run_blocks(self):
        path = self.root / "scenarios" / "note-capture-offline.yaml"
        path.write_text(path.read_text() + "\n# edited after the run\n")
        self.assertBlocks(self.gate(), "has changed since the run was sealed")

    def test_missing_scenario_file_blocks(self):
        (self.root / "scenarios" / "note-capture-offline.yaml").unlink()
        self.assertBlocks(self.gate(), "no scenario")

    def test_version_mismatch_blocks(self):
        manifest = self.read("run.json")
        manifest["scenario"]["version"] = "1.0.1"
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "scenario version is 1.0.0")

    def test_sealed_perturbations_differing_from_the_scenario_block(self):
        """A manifest with the right digest but an easier set of interruptions."""
        manifest = self.read("run.json")
        manifest["scenario"]["perturbations"] = manifest["scenario"]["perturbations"][:1]
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "scenario.perturbations does not match")

    def test_sealed_task_differing_from_the_scenario_blocks(self):
        manifest = self.read("run.json")
        manifest["scenario"]["task"] = "Open the app and look at the list of notes."
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "scenario.task does not match")

    def test_persona_criteria_in_a_scenario_run_block(self):
        manifest = self.read("run.json")
        manifest["stopping_conditions"]["goal_satisfaction"]["success_criteria"] = [
            "I completed the lookup without leaving the app"
        ]
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "not the scenario's")

    def test_criteria_claiming_a_scenario_that_is_not_bound_blocks(self):
        manifest = self.read("run.json")
        del manifest["scenario"]
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "binds none")

    def test_out_of_scope_surface_warns_but_passes(self):
        records = self.session()
        records[self.index_of(records, "outcome", 1)]["action"]["surface"] = "settings/account"
        self.write_session(records)
        report = self.gate()
        self.assertPasses(report)
        self.assertWarns(report, "outside the scenario's in_scope_surfaces")


# --------------------------------------------------------------------------
# perturbations
# --------------------------------------------------------------------------

class TestPerturbations(ScenarioRunCase):
    def test_due_perturbation_never_applied_blocks(self):
        records = self.session()
        del records[self.index_of(records, "perturbation", index=1)]
        self.write_session(records)
        self.assertBlocks(self.gate(), "was due but never applied")

    def test_perturbation_not_yet_due_needs_no_record(self):
        """A session that ends before the step is reached owes nothing."""
        from pqa import gate
        from pqa.core import Report

        records = [r for r in self.session() if r.get("step", 0) <= 5 or r["kind"] == "terminated"]
        report = Report()
        gate._check_perturbations(self.read("run.json"), records, report)
        self.assertTrue(report.ok, [p.message for p in report.errors])

    def test_perturbation_due_at_the_final_step_needs_no_record(self):
        from pqa import gate
        from pqa.core import Report

        records = [r for r in self.session() if r.get("step", 0) <= 6 and r["kind"] != "perturbation"
                   or r["kind"] == "perturbation" and r["index"] == 0]
        report = Report()
        gate._check_perturbations(self.read("run.json"), records, report)
        self.assertTrue(report.ok, [p.message for p in report.errors])

    def test_wrong_perturbation_kind_blocks(self):
        records = self.session()
        records[self.index_of(records, "perturbation", index=0)]["do"] = "rotate"
        self.write_session(records)
        self.assertBlocks(self.gate(), "but the sealed perturbation is 'offline'")

    def test_fabricated_perturbation_blocks(self):
        records = self.session()
        forged = dict(records[self.index_of(records, "perturbation", index=0)], index=5)
        records.insert(self.index_of(records, "intent", 4), forged)
        self.write_session(records)
        self.assertBlocks(self.gate(), "but the scenario seals 2")

    def test_duplicate_perturbation_blocks(self):
        records = self.session()
        position = self.index_of(records, "perturbation", index=0)
        records.insert(position + 1, dict(records[position]))
        self.write_session(records)
        self.assertBlocks(self.gate(), "recorded more than once")

    def test_late_perturbation_blocks(self):
        """Steps 3 and 4 ran online, so the scenario's offline save never happened."""
        records = self.session()
        perturbation = records.pop(self.index_of(records, "perturbation", index=0))
        perturbation["step"] = 4
        records.insert(self.index_of(records, "outcome", 4) + 1, perturbation)
        self.write_session(records)
        self.assertBlocks(self.gate(), "applied late")

    def test_perturbation_moved_after_the_next_step_blocks_even_with_the_right_step(self):
        records = self.session()
        perturbation = records.pop(self.index_of(records, "perturbation", index=0))
        records.insert(self.index_of(records, "outcome", 3) + 1, perturbation)
        self.write_session(records)
        self.assertBlocks(self.gate(), "applied late")

    def test_early_perturbation_blocks(self):
        records = self.session()
        perturbation = records.pop(self.index_of(records, "perturbation", index=0))
        records.insert(self.index_of(records, "outcome", 2), perturbation)
        self.write_session(records)
        self.assertBlocks(self.gate(), "before step 2 was recorded")

    def test_perturbation_from_another_scenario_version_blocks(self):
        records = self.session()
        records[self.index_of(records, "perturbation", index=0)]["scenario_digest"] = "sha256:" + "0" * 64
        self.write_session(records)
        self.assertBlocks(self.gate(), "digest does not match")

    def test_perturbation_in_a_run_without_a_scenario_blocks(self):
        manifest = self.read("run.json")
        records = self.session()
        del manifest["scenario"]
        manifest["stopping_conditions"]["goal_satisfaction"].pop("criteria_source")
        manifest["stopping_conditions"]["goal_satisfaction"]["success_criteria"] = [
            "I completed the lookup without leaving the app"
        ]
        self.write("run.json", manifest)
        self.reseal()
        self.write_session(records)
        self.assertBlocks(self.gate(), "in a run with no scenario")


# --------------------------------------------------------------------------
# two-phase steps
# --------------------------------------------------------------------------

class TestTwoPhaseSteps(ScenarioRunCase):
    def test_outcome_without_an_intent_blocks(self):
        records = self.session()
        del records[self.index_of(records, "intent", 4)]
        self.write_session(records)
        self.assertBlocks(self.gate(), "outcome has no intent written before it")

    def test_intent_written_after_its_outcome_blocks(self):
        records = self.session()
        i, o = self.index_of(records, "intent", 4), self.index_of(records, "outcome", 4)
        records[i], records[o] = records[o], records[i]
        self.write_session(records)
        self.assertBlocks(self.gate(), "outcome has no intent written before it")

    def test_intent_timestamped_after_its_outcome_blocks(self):
        records = self.session()
        records[self.index_of(records, "intent", 4)]["ts"] = "2026-08-22T09:00:00Z"
        self.write_session(records)
        self.assertBlocks(self.gate(), "timestamped after its outcome")

    def test_intent_with_no_outcome_blocks(self):
        records = self.session()
        del records[self.index_of(records, "outcome", 3)]
        self.write_session(records)
        self.assertBlocks(self.gate(), "intent has no outcome")

    def test_interleaved_steps_block(self):
        records = self.session()
        intent4 = records.pop(self.index_of(records, "intent", 4))
        records.insert(self.index_of(records, "outcome", 3), intent4)
        self.write_session(records)
        self.assertBlocks(self.gate(), "intent has no outcome before the next step")

    def test_second_intent_for_a_step_blocks(self):
        """Revising the expectation once the result is in sight."""
        records = self.session()
        position = self.index_of(records, "intent", 4)
        revised = dict(records[position], expected="nothing, it never says anything")
        records.insert(self.index_of(records, "outcome", 4), revised)
        self.write_session(records)
        self.assertBlocks(self.gate(), "more than one intent")

    def test_mixing_observation_and_intent_for_one_step_blocks(self):
        records = self.session()
        intent = records[self.index_of(records, "intent", 4)]
        outcome = records[self.index_of(records, "outcome", 4)]
        observation = {k: v for k, v in outcome.items()}
        observation.update(kind="observation", expected=intent["expected"])
        records.insert(self.index_of(records, "intent", 4), observation)
        self.write_session(records)
        self.assertBlocks(self.gate(), "both as an observation and as intent/outcome")

    def test_outcome_cannot_restate_the_expectation(self):
        records = self.session()
        records[self.index_of(records, "outcome", 4)]["expected"] = "exactly what happened"
        self.write_session(records)
        self.assertBlocks(self.gate(), "outcome carries `expected`")

    def test_trailing_intent_before_termination_is_allowed(self):
        from pqa import validate

        records = self.session()
        del records[self.index_of(records, "outcome", 8)]
        report = validate._check_two_phase(records, "session.jsonl")
        self.assertTrue(report.ok, [p.message for p in report.errors])

    def test_action_changed_between_intent_and_outcome_warns(self):
        records = self.session()
        records[self.index_of(records, "outcome", 5)]["action"]["class"] = "tap"
        self.write_session(records)
        report = self.gate()
        self.assertPasses(report)
        self.assertWarns(report, "intent planned a 'scroll' action")

    def test_outcomes_count_against_the_budget(self):
        manifest = self.read("run.json")
        manifest["stopping_conditions"]["step_budget"] = 7
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "8 observations exceed the sealed step budget of 7")

    def test_evidence_resolves_against_outcome_steps(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["evidence_refs"] = [{"kind": "session_log", "steps": [4, 9]}]
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "do not exist")


# --------------------------------------------------------------------------
# the live hook
# --------------------------------------------------------------------------

class TestPerturbTick(KitTestCase):
    """drift-tick injects sealed perturbations when their step is reached."""

    def setUp(self):
        super().setUp()
        self.out = self.root / "runs" / "2026-09-01-offline"
        result = pqa(
            "new-run", "--persona", "time-starved-parent", "--scenario", "note-capture-offline",
            "--target", "notes-app", "--build", "c0ffee1", "--run-id", "2026-09-01-offline",
            "--out", str(self.out), home=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pqa("seal", str(self.out), home=self.root).returncode, 0)
        (self.out / "session.jsonl").write_text("")

    def step(self, n, phase):
        record = {"kind": phase, "step": n, "ts": f"2026-09-01T10:00:{n:02d}Z",
                  "action": {"class": "tap", "target": "the plus sign", "surface": "notes/list"}}
        if phase == "intent":
            record["expected"] = "a blank page to type on"
        else:
            record.update(observed="a blank page came up", felt="fine")
        with (self.out / "session.jsonl").open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def tick(self):
        from pqa import cli

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["drift-tick", str(self.out)])
        self.assertEqual(code, 0)
        text = stdout.getvalue().strip()
        return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else None

    def kinds(self):
        lines = (self.out / "session.jsonl").read_text().splitlines()
        return [json.loads(line)["kind"] for line in lines]

    def test_injects_once_when_the_step_is_recorded(self):
        self.step(1, "intent"); self.assertIsNone(self.tick())
        self.step(1, "outcome"); self.assertIsNone(self.tick())
        self.step(2, "intent")
        self.assertIsNone(self.tick(), "an intent is not a recorded step")
        self.step(2, "outcome")

        context = self.tick()
        self.assertIn("Turn the device's connection off", context)
        self.assertIn("until you have recorded step 5", context)
        self.assertIn("the train goes into a tunnel", context)

        record = json.loads((self.out / "session.jsonl").read_text().splitlines()[-1])
        self.assertEqual(
            {k: record[k] for k in ("kind", "step", "index", "at_step", "do", "for")},
            {"kind": "perturbation", "step": 2, "index": 0, "at_step": 2,
             "do": "offline", "for": "3 steps"},
        )
        self.assertEqual(record["scenario_digest"], self.read_manifest()["scenario"]["scenario_digest"])

        self.assertIsNone(self.tick(), "a perturbation is injected once")
        self.assertEqual(self.kinds().count("perturbation"), 1)

    def test_a_missed_tick_injects_late_and_says_so(self):
        for n in (1, 2, 3):
            self.step(n, "intent"); self.step(n, "outcome")
        self.assertIsNotNone(self.tick())
        record = json.loads((self.out / "session.jsonl").read_text().splitlines()[-1])
        self.assertEqual((record["step"], record["at_step"]), (3, 2))

    def test_reinjection_uses_the_scenario_task_not_the_card_goals(self):
        for n in range(1, 9):
            self.step(n, "intent"); self.step(n, "outcome")
        context = self.tick()
        self.assertIn("You are trying to: Write down something you need to remember", context)
        self.assertNotIn("before someone needs me again", context)
        # Both due perturbations and the persona block arrive in one envelope.
        self.assertIn("perturbation 2 of 2", context)
        self.assertEqual(self.kinds().count("persona_injection"), 1)

    def read_manifest(self):
        return json.loads((self.out / "run.json").read_text())


# --------------------------------------------------------------------------
# new-run and loading
# --------------------------------------------------------------------------

class TestNewRunWithScenario(KitTestCase):
    def new_run(self, *extra, persona="cautious-first-timer", run_id="2026-09-01-sc"):
        out = self.root / "runs" / run_id
        result = pqa(
            "new-run", "--persona", persona, "--target", "notes-app", "--build", "abc1234",
            "--run-id", run_id, "--out", str(out), *extra, home=self.root,
        )
        return result, out

    def test_binds_the_scenario_and_takes_its_criteria(self):
        result, out = self.new_run("--scenario", "note-capture-offline")
        self.assertEqual(result.returncode, 0, result.stderr)

        from pqa import scenarios

        scenario = scenarios.load("note-capture-offline")
        manifest = json.loads((out / "run.json").read_text())
        self.assertEqual(manifest["scenario"]["scenario_digest"], scenario.digest)
        self.assertEqual(manifest["scenario"]["perturbations"], scenario.perturbations)
        goal = manifest["stopping_conditions"]["goal_satisfaction"]
        self.assertEqual(goal["success_criteria"], scenario.success_criteria)
        self.assertEqual(goal["criteria_source"], "scenario")
        self.assertEqual(
            manifest["stopping_conditions"]["scope_bounds"]["in_scope_surfaces"],
            ["notes/list", "notes/editor"],
        )

        self.assertEqual(pqa("seal", str(out), home=self.root).returncode, 0)
        lock = json.loads((out / "run.lock").read_text())
        self.assertEqual(lock["scenario_digest"], scenario.digest)

    def test_without_a_scenario_the_card_criteria_are_the_fallback(self):
        result, out = self.new_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads((out / "run.json").read_text())
        self.assertNotIn("scenario", manifest)
        self.assertEqual(manifest["stopping_conditions"]["goal_satisfaction"]["criteria_source"], "persona")

    def test_unknown_scenario_fails(self):
        result, _ = self.new_run("--scenario", "nothing-like-this")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no scenario", result.stderr)

    def test_perturbation_beyond_the_budget_fails(self):
        result, _ = self.new_run("--scenario", "note-capture-offline", "--budget", "5")
        self.assertEqual(result.returncode, 1)
        self.assertIn("can never reach", result.stderr)

    def test_scenario_for_another_target_warns(self):
        result, _ = self.new_run("--scenario", "checkout-interrupted")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("written for 'shop-app'", result.stderr)

    def test_scenario_text_using_the_personas_forbidden_words_warns(self):
        path = self.root / "scenarios" / "note-capture-offline.yaml"
        path.write_text(path.read_text().replace(
            "make sure it is still there afterwards", "check the API kept it"))
        result, _ = self.new_run("--scenario", "note-capture-offline", persona="time-starved-parent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uses 'API'", result.stderr)


class TestScenarioFiles(KitTestCase):
    def write_scenario(self, name, text):
        (self.root / "scenarios" / f"{name}.yaml").write_text(text)

    BASE = (
        "id: {id}\nversion: 1.0.0\ntitle: A scenario\n"
        "task: Do the thing that this scenario is about\n"
        "entry:\n  surface: home\n"
        "success_criteria:\n  - The thing is visibly done\n"
        "in_scope_surfaces:\n  - home\n"
    )

    def test_shipped_scenarios_load(self):
        from pqa import scenarios

        ids = scenarios.list_ids()
        self.assertIn("checkout-interrupted", ids)
        self.assertIn("note-capture-offline", ids)
        for scenario_id in ids:
            scenarios.load(scenario_id)

    def test_perturbations_out_of_order_are_rejected(self):
        from pqa import scenarios
        from pqa.core import PqaError

        self.write_scenario("bad-order", self.BASE.format(id="bad-order") + (
            "perturbations:\n"
            "  - at_step: 5\n    do: rotate\n"
            "  - at_step: 3\n    do: kill\n"
        ))
        with self.assertRaisesRegex(PqaError, "strictly increasing"):
            scenarios.load("bad-order")
        self.assertEqual(pqa("validate", "--self-check", home=self.root).returncode, 2)

    def test_unknown_perturbation_is_rejected(self):
        from pqa import scenarios
        from pqa.core import PqaError

        self.write_scenario("bad-kind", self.BASE.format(id="bad-kind") + (
            "perturbations:\n  - at_step: 2\n    do: earthquake\n"
        ))
        with self.assertRaisesRegex(PqaError, "invalid"):
            scenarios.load("bad-kind")

    def test_id_must_match_filename(self):
        from pqa import scenarios
        from pqa.core import PqaError

        self.write_scenario("one-name", self.BASE.format(id="another-name"))
        with self.assertRaisesRegex(PqaError, "does not match filename"):
            scenarios.load("one-name")

    def test_scenarios_command_lists_them(self):
        result = pqa("scenarios", home=self.root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("offline@2", result.stdout)


if __name__ == "__main__":
    unittest.main()
