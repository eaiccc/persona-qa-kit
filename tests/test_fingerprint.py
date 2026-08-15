"""Fingerprints are the basis of cross-run comparison, so they must be stable."""

from __future__ import annotations

import unittest

from support import KIT  # noqa: F401  (sets sys.path)

from pqa import fingerprint


class TestNormalization(unittest.TestCase):
    def test_separator_variants_collapse(self):
        variants = [
            "Settings / Notifications",
            "settings/notifications",
            "settings-notifications",
            "  Settings   Notifications  ",
            "SETTINGS//NOTIFICATIONS",
        ]
        fingerprints = {fingerprint.compute(v, "tap", "no_feedback") for v in variants}
        self.assertEqual(
            len(fingerprints), 1,
            "surface variants must not each produce a new defect identity",
        )

    def test_normalize_surface_output(self):
        self.assertEqual(
            fingerprint.normalize_surface("Settings / Notifications"),
            "settings/notifications",
        )

    def test_distinct_surfaces_stay_distinct(self):
        a = fingerprint.compute("notes/editor", "tap", "crash")
        b = fingerprint.compute("notes/list", "tap", "crash")
        self.assertNotEqual(a, b)

    def test_distinct_symptoms_stay_distinct(self):
        a = fingerprint.compute("notes/editor", "tap", "crash")
        b = fingerprint.compute("notes/editor", "tap", "data_loss")
        self.assertNotEqual(a, b)


class TestStability(unittest.TestCase):
    def test_golden_value(self):
        """Pinned. Changing this breaks every stored finding's identity.

        If this test fails you have changed the fingerprint algorithm, which
        silently turns every open defect into a new one. That needs a version
        bump on the `pqa1:` prefix, not a new expected value here.
        """
        self.assertEqual(
            fingerprint.compute("notes/editor", "background", "data_loss"),
            "pqa1:305363d41af26da9",
        )

    def test_format(self):
        value = fingerprint.compute("a/b", "tap", "crash")
        self.assertTrue(value.startswith("pqa1:"))
        self.assertEqual(len(value), len("pqa1:") + 16)


class TestVerify(unittest.TestCase):
    def _finding(self, **overrides):
        base = {
            "surface": "notes/editor",
            "action_class": "submit",
            "symptom_class": "no_feedback",
        }
        base["fingerprint"] = fingerprint.compute(
            base["surface"], base["action_class"], base["symptom_class"]
        )
        base.update(overrides)
        return base

    def test_matching_fingerprint_verifies(self):
        ok, _ = fingerprint.verify(self._finding())
        self.assertTrue(ok)

    def test_hand_written_fingerprint_fails(self):
        ok, expected = fingerprint.verify(self._finding(fingerprint="pqa1:0000000000000000"))
        self.assertFalse(ok)
        self.assertTrue(expected.startswith("pqa1:"))

    def test_edited_surface_without_refingerprint_fails(self):
        ok, _ = fingerprint.verify(self._finding(surface="notes/somewhere-else"))
        self.assertFalse(ok)

    def test_missing_input_raises(self):
        with self.assertRaises(KeyError):
            fingerprint.for_finding({"surface": "a", "action_class": "tap"})


if __name__ == "__main__":
    unittest.main()
