"""The drift tripwire has to be predictable, or it gets muted."""

from __future__ import annotations

import unittest

from support import KIT  # noqa: F401  (sets sys.path)

from pqa import drift


def observation(step: int, **fields):
    record = {
        "kind": "observation",
        "step": step,
        "ts": "2026-08-15T09:00:00Z",
        "action": {"class": "tap", "target": "a button"},
        "expected": "something",
        "observed": "something else",
        "felt": "fine",
    }
    record.update(fields)
    return record


class TestTermMatching(unittest.TestCase):
    def test_whole_words_only(self):
        pattern = drift.term_pattern("cache")
        self.assertTrue(pattern.search("clear the cache"))
        self.assertFalse(pattern.search("cachet"))
        self.assertFalse(pattern.search("uncached"))

    def test_case_insensitive(self):
        self.assertTrue(drift.term_pattern("API").search("the api returned nothing"))

    def test_multi_word_tolerates_separators(self):
        pattern = drift.term_pattern("race condition")
        self.assertTrue(pattern.search("looks like a race condition"))
        self.assertTrue(pattern.search("a race-condition maybe"))
        self.assertFalse(pattern.search("race and condition are separate words here"))

    def test_simple_plural(self):
        self.assertTrue(drift.term_pattern("API").search("both APIs failed"))
        self.assertTrue(drift.term_pattern("endpoint").search("the endpoints are down"))

    def test_no_stemming(self):
        # Deliberately not matched: predictability beats recall for a tripwire.
        self.assertFalse(drift.term_pattern("cache").search("caching is broken"))

    def test_cjk_text_has_no_word_boundaries(self):
        # Chinese is written without spaces. Treating CJK characters as word
        # characters made every term unmatchable in a Chinese session log.
        self.assertTrue(drift.term_pattern("模型").search("這個模型認錯字了"))
        self.assertTrue(drift.term_pattern("OCR").search("應該是OCR辨識錯"))
        self.assertTrue(drift.term_pattern("prompt").search("改一下prompt就好"))
        self.assertFalse(drift.term_pattern("API").search("RAPID"))

    def test_empty_term_rejected(self):
        with self.assertRaises(ValueError):
            drift.term_pattern("   ")


class TestScan(unittest.TestCase):
    def test_clean_log_has_no_violations(self):
        records = [observation(1, felt="my note is gone")]
        self.assertEqual(drift.scan(records, ["backend", "API"]), [])

    def test_finds_the_term_and_the_field(self):
        records = [observation(4, felt="the backend probably fell over")]
        violations = drift.scan(records, ["backend"])
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].step, 4)
        self.assertEqual(violations[0].term, "backend")
        self.assertEqual(violations[0].field, "felt")
        self.assertIn("backend", violations[0].quote)

    def test_scans_expected_and_observed(self):
        records = [
            observation(1, expected="the API would respond"),
            observation(2, observed="a race condition, probably"),
        ]
        violations = drift.scan(records, ["API", "race condition"])
        self.assertEqual({v.field for v in violations}, {"expected", "observed"})

    def test_scans_action_fields(self):
        records = [observation(1, action={"class": "tap", "target": "the retry endpoint"})]
        violations = drift.scan(records, ["endpoint"])
        self.assertEqual(violations[0].field, "action.target")

    def test_scans_blocked_notes(self):
        records = [{
            "kind": "blocked", "step": 9, "ts": "2026-08-15T09:00:00Z",
            "repeated_steps": [7, 8], "note": "stuck, maybe a cache invalidation issue",
        }]
        self.assertEqual(len(drift.scan(records, ["cache invalidation"])), 1)

    def test_ignores_non_dict_records(self):
        self.assertEqual(drift.scan(["not a record", None], ["backend"]), [])


class TestCadence(unittest.TestCase):
    def _log(self, injections):
        records = [observation(i) for i in range(1, 25)]
        for step in injections:
            records.append({
                "kind": "persona_injection", "step": step,
                "ts": "2026-08-15T09:00:00Z", "persona_id": "x",
                "card_digest": "sha256:" + "0" * 64,
            })
        return records

    def test_no_gaps_when_cadence_is_met(self):
        self.assertEqual(drift.cadence_gaps(self._log([8, 16, 24]), 8, 24), [])

    def test_reports_missing_injections(self):
        self.assertEqual(drift.cadence_gaps(self._log([8]), 8, 24), [16, 24])

    def test_tolerates_off_by_one(self):
        # The hook fires after the step, so landing a step late is normal.
        self.assertEqual(drift.cadence_gaps(self._log([9, 17, 25]), 8, 24), [])

    def test_injection_steps_are_sorted(self):
        self.assertEqual(drift.injection_steps(self._log([16, 8])), [8, 16])

    def test_zero_cadence_is_inert(self):
        self.assertEqual(drift.cadence_gaps(self._log([]), 0, 24), [])


if __name__ == "__main__":
    unittest.main()
