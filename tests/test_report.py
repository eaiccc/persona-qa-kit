"""`pqa report` and `pqa check-improvements`, against both example runs."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import unittest

from support import KIT, SCENARIO_EXAMPLE, KitTestCase

PQA = str(KIT / "bin" / "pqa")
NO_FEEDBACK = "pqa1:5c9768281b4dc1ac"   # both runs
BACKGROUND_LOSS = "pqa1:305363d41af26da9"  # first run only
OFFLINE_LOSS = "pqa1:84852121b0795bed"     # second run only


def pqa(*args, home):
    env = dict(os.environ, NO_COLOR="1", PQA_HOME=str(home))
    return subprocess.run([PQA, *args], capture_output=True, text=True, env=env, cwd=str(KIT))


class ReportCase(KitTestCase):
    """Both example runs side by side in the temp kit's runs/."""

    def setUp(self):
        super().setUp()
        self.second = self.root / "runs" / SCENARIO_EXAMPLE.name
        shutil.copytree(SCENARIO_EXAMPLE, self.second)
        shutil.copy(KIT / "examples" / "improvements.json", self.root / "improvements.json")
        # The coverage matrix lists every persona card, so pin the set these
        # expectations were written for; shipping a new card is not a regression.
        fixtures = {"cautious-first-timer", "screen-reader-commuter", "time-starved-parent"}
        for card in (self.root / "personas").glob("*.yaml"):
            if card.stem not in fixtures:
                card.unlink()

    def build(self, **kwargs):
        from pqa import report

        return report.build(self.root / "runs", **kwargs)

    def defect(self, data, fingerprint):
        return next(d for d in data["defects"] if d["fingerprint"] == fingerprint)


class TestAggregation(ReportCase):
    def test_recurring_defect_is_merged_across_personas_and_builds(self):
        d = self.defect(self.build(), NO_FEEDBACK)
        self.assertEqual(d["runs"], 2)
        self.assertEqual(d["personas"], ["screen-reader-commuter", "time-starved-parent"])
        self.assertEqual(d["scenarios"], [None, "note-capture-offline"])
        self.assertEqual(d["first_seen_build"], "a1b2c3d")
        self.assertEqual(d["builds"], ["a1b2c3d", "b7e9f01"])
        self.assertEqual(d["latest_build_status"], "present")

    def test_absence_on_the_latest_build_is_not_reported_as_fixed(self):
        """No run on b7e9f01 covered the cell where the background loss was seen."""
        self.assertEqual(
            self.defect(self.build(), BACKGROUND_LOSS)["latest_build_status"], "not re-tested"
        )

    def test_absence_after_a_retest_is_reported_as_not_seen(self):
        from pqa import report

        defects = [{
            "target": "t", "fingerprint": "pqa1:0000000000000000", "builds": ["old"],
            "occurrences": [{"persona": "p", "scenario": "s"}],
        }]
        included = [{"target": "t", "build": "new", "persona": "p", "scenario": "s"}]
        report._latest_build_status(defects, included, [{"target": "t", "latest_build": "new"}])
        self.assertEqual(defects[0]["latest_build_status"], "not seen")

    def test_only_accepted_findings_count(self):
        fingerprints = {d["fingerprint"] for d in self.build()["defects"]}
        self.assertEqual(fingerprints, {NO_FEEDBACK, BACKGROUND_LOSS, OFFLINE_LOSS})
        # merged F-003 and rejected F-004 from the second run are absent
        self.assertNotIn("pqa1:d886a8ac44cd2caf", fingerprints)
        self.assertNotIn("pqa1:2020b41b8e57022b", fingerprints)

    def test_ranked_by_severity_then_spread(self):
        order = [d["fingerprint"] for d in self.build()["defects"]]
        self.assertEqual(order, [BACKGROUND_LOSS, OFFLINE_LOSS, NO_FEEDBACK])

    def test_suggestions_quote_user_voice_verbatim(self):
        data = self.build()
        suggestion = next(s for s in data["suggestions"] if s["fingerprint"] == NO_FEEDBACK)
        quotes = [q["quote"] for q in suggestion["quotes"]]
        self.assertEqual(quotes, [
            "I have no idea if any of this is saved. I will sort it out later",
            "it did not read anything out. no idea if that saved",
        ])
        self.assertIn("notes/editor", suggestion["suggestion"])


class TestExclusion(ReportCase):
    def test_a_run_that_fails_the_gate_is_excluded(self):
        (self.run_path / "findings.reviewed.json").unlink()
        data = self.build()
        run = next(r for r in data["runs"] if r["run_id"] == self.run_path.name)
        self.assertFalse(run["included"])
        self.assertIn("failed the review gate", run["excluded_because"])
        self.assertNotIn(BACKGROUND_LOSS, {d["fingerprint"] for d in data["defects"]})

    def test_include_ungated_overrides(self):
        # Break the gate without removing the findings themselves.
        path = self.root / "personas" / "time-starved-parent.yaml"
        path.write_text(path.read_text() + "\n# edited\n")
        self.assertNotIn(BACKGROUND_LOSS, {d["fingerprint"] for d in self.build()["defects"]})
        data = self.build(include_ungated=True)
        self.assertIn(BACKGROUND_LOSS, {d["fingerprint"] for d in data["defects"]})

    def test_untrustworthy_runs_are_excluded(self):
        evaluation = self.read("evaluation.json")
        evaluation["trust"]["verdict"] = "untrustworthy"
        self.write("evaluation.json", evaluation)
        run = next(r for r in self.build()["runs"] if r["run_id"] == self.run_path.name)
        self.assertFalse(run["included"])


class TestCoverage(ReportCase):
    def test_matrix_and_gaps(self):
        (table,) = self.build()["coverage"]
        self.assertEqual(table["target"], "notes-app")
        self.assertEqual(table["latest_build"], "b7e9f01")
        # checkout-interrupted is written for shop-app, so it is not a notes-app gap
        self.assertEqual(table["scenarios"], ["note-capture-offline", None])
        self.assertEqual(table["gaps"], [
            {"persona": "cautious-first-timer", "scenario": "note-capture-offline"},
            {"persona": "time-starved-parent", "scenario": "note-capture-offline"},
        ])
        cell = next(c for c in table["cells"]
                    if c["persona"] == "screen-reader-commuter" and c["scenario"] == "note-capture-offline")
        self.assertEqual((cell["runs"], cell["accepted"]), (1, 2))
        self.assertEqual(len(table["coverage_notes"]), 2)


class TestCommand(ReportCase):
    def test_markdown(self):
        result = pqa("report", str(self.root / "runs"), home=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        for needle in (
            "# Persona QA report",
            "## Improvement suggestions",
            '> "it did not read anything out. no idea if that saved"',
            "## Coverage: notes-app (latest build b7e9f01)",
            "**Gaps (2):**",
            "not re-tested",
        ):
            self.assertIn(needle, result.stdout)

    def test_json_and_output_files(self):
        out = self.root / "report.md"
        side = self.root / "report.json"
        result = pqa("report", str(self.root / "runs"), "-o", str(out), "--json", str(side),
                     home=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# Persona QA report", out.read_text())
        self.assertEqual(len(json.loads(side.read_text())["defects"]), 3)

        result = pqa("report", str(self.root / "runs"), "--format", "json", home=self.root)
        self.assertEqual(json.loads(result.stdout)["runs"][0]["run_id"], "2026-08-15-notes-app")

    def test_shipped_examples(self):
        result = pqa("report", str(KIT / "examples" / "runs"), home=KIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 of 2 run(s) included", result.stdout)


class TestCheckImprovements(ReportCase):
    def check(self, data=None):
        path = self.root / "improvements.json"
        if data is not None:
            path.write_text(json.dumps(data))
        return pqa("check-improvements", str(path), str(self.root / "runs"), home=self.root)

    def shipped(self):
        return json.loads((KIT / "examples" / "improvements.json").read_text())

    def test_shipped_example_passes(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_paraphrased_quote_fails(self):
        data = self.shipped()
        data["recommendations"][1]["user_voice"][1]["quote"] = "the app gave no audio confirmation"
        result = self.check(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("is not in any user_voice", result.stdout)

    def test_citing_a_rejected_finding_fails(self):
        data = self.shipped()
        data["recommendations"][1]["addresses"].append("pqa1:2020b41b8e57022b")
        result = self.check(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an accepted finding", result.stdout)

    def test_naming_a_persona_that_never_hit_it_fails(self):
        data = self.shipped()
        data["recommendations"][1]["personas"].append("cautious-first-timer")
        result = self.check(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hit none of the cited defects", result.stdout)

    def test_dropping_a_blocker_silently_fails(self):
        data = self.shipped()
        rec = data["recommendations"][0]
        rec["addresses"] = [OFFLINE_LOSS]
        rec["user_voice"] = [q for q in rec["user_voice"] if q["fingerprint"] == OFFLINE_LOSS]
        rec["personas"] = ["screen-reader-commuter"]
        result = self.check(data)
        self.assertEqual(result.returncode, 2)
        self.assertIn(BACKGROUND_LOSS, result.stdout)

        explained = copy.deepcopy(data)
        explained["not_addressed"] = [{
            "fingerprint": BACKGROUND_LOSS,
            "reason": "already scheduled with the lifecycle rework",
        }]
        self.assertEqual(self.check(explained).returncode, 0)

    def test_schema_is_enforced(self):
        data = self.shipped()
        data["generated_by"] = "qa-agent"
        self.assertEqual(self.check(data).returncode, 2)


if __name__ == "__main__":
    unittest.main()
