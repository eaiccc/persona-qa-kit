"""Every gate check, verified by breaking exactly one thing at a time.

A check nobody has watched fail is a check you do not know you have. Each test
here starts from the example run -- which passes -- introduces one specific
defect, and asserts the gate refuses. If a test ever starts passing without its
assertion firing, the constraint it covers has quietly become decorative.
"""

from __future__ import annotations

import unittest

from support import KitTestCase


class TestCleanRun(KitTestCase):
    def test_example_run_passes(self):
        self.assertPasses(self.gate())

    def test_example_run_runs_a_meaningful_number_of_checks(self):
        # Guards against a refactor that silently short-circuits the gate.
        self.assertGreater(self.gate().checks_run, 30)


class TestReviewIsMandatory(KitTestCase):
    """DESIGN.md section 1: the agent cannot decline to be reviewed."""

    def test_missing_review_blocks(self):
        (self.run_path / "findings.reviewed.json").unlink()
        self.assertBlocks(self.gate(), "missing")

    def test_unreviewed_candidate_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["reviews"] = [r for r in reviewed["reviews"] if r["finding_id"] != "F-004"]
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "F-004")

    def test_review_of_a_candidate_that_was_never_proposed_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["reviews"].append({
            "finding_id": "F-999",
            "verdict": "rejected",
            "rejection_reason": "not_a_defect",
            "rationale": "this candidate does not exist anywhere in the input file",
        })
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "never proposed")

    def test_stale_review_blocks(self):
        """A review carried over from a different candidate set."""
        candidates = self.read("findings.candidates.json")
        candidates["coverage_note"] = "edited after the review was written"
        self.write("findings.candidates.json", candidates)
        # deliberately do NOT redigest
        self.assertBlocks(self.gate(), "candidates_digest")

    def test_rejection_without_a_reason_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        for review in reviewed["reviews"]:
            review.pop("rejection_reason", None)
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "rejection_reason")

    def test_empty_rationale_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["reviews"][0]["rationale"] = "fine"
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "shorter than the minimum")


class TestAcceptedListIntegrity(KitTestCase):
    """The reviewer adjusts severity and confidence; it does not rewrite defects."""

    def test_accepted_list_disagreeing_with_verdicts_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["accepted"] = reviewed["accepted"][:1]
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "omits")

    def test_smuggling_a_rejected_finding_into_accepted_blocks(self):
        candidates = self.read("findings.candidates.json")
        rejected = next(f for f in candidates["findings"] if f["id"] == "F-003")
        reviewed = self.read("findings.reviewed.json")
        reviewed["accepted"].append(rejected)
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "do not mark as accepted")

    def test_severity_not_matching_the_review_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["accepted"][0]["severity"] = "cosmetic"
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "severity")

    def test_redefining_the_defect_blocks(self):
        reviewed = self.read("findings.reviewed.json")
        reviewed["accepted"][0]["fingerprint"] = "pqa1:0000000000000000"
        self.write("findings.reviewed.json", reviewed)
        self.redigest()
        self.assertBlocks(self.gate(), "fingerprint")


class TestEvidenceMustResolve(KitTestCase):
    """DESIGN.md section 2: the cheapest available guard against fabrication."""

    def test_citing_a_step_that_does_not_exist_blocks(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["evidence_refs"] = [
            {"kind": "session_log", "steps": [3, 97]}
        ]
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "do not exist")

    def test_citing_a_missing_artifact_blocks(self):
        (self.run_path / "screenshots" / "step-005.png").unlink()
        self.assertBlocks(self.gate(), "does not exist")

    def test_artifact_path_escaping_the_run_directory_blocks(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["evidence_refs"] = [
            {"kind": "artifact", "path": "../../../etc/passwd"}
        ]
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "escapes")

    def test_finding_with_no_evidence_blocks(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["evidence_refs"] = []
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "minimum is 1")


class TestFingerprintIsDerived(KitTestCase):
    def test_hand_written_fingerprint_blocks(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["fingerprint"] = "pqa1:deadbeefdeadbeef"
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "does not match the value computed")

    def test_changing_surface_without_refingerprinting_blocks(self):
        candidates = self.read("findings.candidates.json")
        candidates["findings"][0]["surface"] = "notes/somewhere-else"
        self.write("findings.candidates.json", candidates)
        self.redigest()
        self.assertBlocks(self.gate(), "fingerprint")


class TestSealedStoppingConditions(KitTestCase):
    """DESIGN.md section 3: conditions fixed before execution, not during."""

    def test_raising_the_budget_after_sealing_blocks(self):
        manifest = self.read("run.json")
        manifest["stopping_conditions"]["step_budget"] = 400
        self.write("run.json", manifest)
        self.assertBlocks(self.gate(), "changed after it was sealed")

    def test_missing_lock_blocks(self):
        (self.run_path / "run.lock").unlink()
        self.assertBlocks(self.gate(), "never sealed")

    def test_relaxing_success_criteria_after_sealing_blocks(self):
        manifest = self.read("run.json")
        # Still schema-valid -- the point is that a *legal* edit is caught too.
        manifest["stopping_conditions"]["goal_satisfaction"]["success_criteria"] = [
            "I opened the app without it crashing"
        ]
        self.write("run.json", manifest)
        self.assertBlocks(self.gate(), "changed after it was sealed")

    def test_exceeding_the_budget_blocks(self):
        manifest = self.read("run.json")
        manifest["stopping_conditions"]["step_budget"] = 4
        self.write("run.json", manifest)
        self.reseal()
        self.assertBlocks(self.gate(), "exceed the sealed step budget")

    def test_claiming_budget_exhaustion_early_blocks(self):
        lines = (self.run_path / "session.jsonl").read_text().splitlines()
        lines[-1] = lines[-1].replace('"reason":"blocked"', '"reason":"step_budget"')
        (self.run_path / "session.jsonl").write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "step_budget")


class TestSessionLogCannotCertify(KitTestCase):
    """DESIGN.md section 1: nowhere to write a certification."""

    def test_severity_field_in_a_session_record_blocks(self):
        path = self.run_path / "session.jsonl"
        lines = path.read_text().splitlines()
        lines[0] = lines[0][:-1] + ',"severity":"minor"}'
        path.write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "no field for a verdict")

    def test_status_field_in_a_session_record_blocks(self):
        path = self.run_path / "session.jsonl"
        lines = path.read_text().splitlines()
        lines[0] = lines[0][:-1] + ',"status":"pass"}'
        path.write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "no field for a verdict")

    def test_missing_termination_record_blocks(self):
        path = self.run_path / "session.jsonl"
        lines = [l for l in path.read_text().splitlines() if '"terminated"' not in l]
        path.write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "no 'terminated' record")

    def test_blocked_termination_without_a_blocked_record_blocks(self):
        path = self.run_path / "session.jsonl"
        lines = [l for l in path.read_text().splitlines() if '"kind":"blocked"' not in l]
        path.write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "no 'blocked' record")

    def test_duplicate_step_numbers_block(self):
        path = self.run_path / "session.jsonl"
        lines = path.read_text().splitlines()
        lines.insert(1, lines[0])
        path.write_text("\n".join(lines) + "\n")
        self.redigest()
        self.assertBlocks(self.gate(), "duplicate")


class TestPersonaBinding(KitTestCase):
    def test_editing_the_card_after_the_run_blocks(self):
        card = self.root / "personas" / "time-starved-parent.yaml"
        card.write_text(card.read_text() + "\n# edited after the run\n")
        self.assertBlocks(self.gate(), "has changed since the run was sealed")

    def test_vocabulary_breach_blocks(self):
        path = self.run_path / "session.jsonl"
        text = path.read_text().replace(
            "I have no idea if any of this is saved",
            "the backend probably threw an error here",
        )
        path.write_text(text)
        self.redigest()
        self.assertBlocks(self.gate(), "vocabulary breach")

    def test_candidates_derived_from_a_different_session_block(self):
        path = self.run_path / "session.jsonl"
        path.write_text(path.read_text().replace("pale grey", "light grey"))
        # deliberately do NOT redigest
        self.assertBlocks(self.gate(), "session_digest")


class TestSelfCertificationSignal(KitTestCase):
    def test_accepting_everything_warns(self):
        reviewed = self.read("findings.reviewed.json")
        candidates = self.read("findings.candidates.json")
        by_id = {f["id"]: f for f in candidates["findings"]}
        for review in reviewed["reviews"]:
            review["verdict"] = "accepted"
            review.pop("rejection_reason", None)
        reviewed["accepted"] = [by_id[r["finding_id"]] for r in reviewed["reviews"]]
        for finding in reviewed["accepted"]:
            if finding["id"] == "F-002":
                finding["confidence"] = "confirmed"
        self.write("findings.reviewed.json", reviewed)
        self.redigest()

        report = self.gate()
        self.assertPasses(report)
        joined = " ".join(p.message for p in report.warnings)
        self.assertIn("accepted all", joined)


if __name__ == "__main__":
    unittest.main()
