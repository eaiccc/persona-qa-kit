"""The fallback YAML parser must agree with PyYAML, or it is a liability.

The kit ships a parser so that the drift check works on a machine with no pip
packages. That only helps if the two produce the same persona card. A fallback
that quietly parses `never_uses` differently would disable the tripwire on
exactly the machines that cannot check.
"""

from __future__ import annotations

import unittest

from support import KIT  # noqa: F401  (sets sys.path)

from pqa import yamlite
from pqa.yamlite import YamlError, _fallback_loads

try:
    import yaml as pyyaml
except ImportError:  # pragma: no cover
    pyyaml = None


class TestAgreementWithPyYAML(unittest.TestCase):
    @unittest.skipIf(pyyaml is None, "PyYAML not installed")
    def test_every_persona_card_and_scenario_parses_identically(self):
        cards = sorted((KIT / "personas").glob("*.yaml"))
        self.assertTrue(cards, "no persona cards to compare")
        cards += sorted((KIT / "scenarios").glob("*.yaml"))
        for card in cards:
            with self.subTest(card=card.name):
                text = card.read_text(encoding="utf-8")
                self.assertEqual(
                    _fallback_loads(text),
                    pyyaml.safe_load(text),
                    f"{card.name}: fallback parser disagrees with PyYAML",
                )

    @unittest.skipIf(pyyaml is None, "PyYAML not installed")
    def test_block_scalar_chomping_matches(self):
        for style in ("|", "|-", "|+", ">", ">-"):
            with self.subTest(style=style):
                text = f"key: {style}\n  first line\n  second line\n\n"
                self.assertEqual(_fallback_loads(text), pyyaml.safe_load(text))


class TestSubset(unittest.TestCase):
    def test_block_mapping(self):
        self.assertEqual(_fallback_loads("a: 1\nb: two\n"), {"a": 1, "b": "two"})

    def test_nested_mapping(self):
        parsed = _fallback_loads("outer:\n  inner:\n    leaf: value\n")
        self.assertEqual(parsed, {"outer": {"inner": {"leaf": "value"}}})

    def test_block_sequence(self):
        self.assertEqual(_fallback_loads("items:\n  - a\n  - b\n"), {"items": ["a", "b"]})

    def test_sequence_at_parent_indent(self):
        self.assertEqual(_fallback_loads("items:\n- a\n- b\n"), {"items": ["a", "b"]})

    def test_sequence_of_mappings(self):
        parsed = _fallback_loads("items:\n  - name: one\n    n: 1\n  - name: two\n    n: 2\n")
        self.assertEqual(parsed, {"items": [{"name": "one", "n": 1}, {"name": "two", "n": 2}]})

    def test_scalar_types(self):
        parsed = _fallback_loads(
            "i: 42\nf: 3.5\nt: true\nf2: false\nn: null\nn2: ~\ns: plain text\n"
        )
        self.assertEqual(parsed["i"], 42)
        self.assertEqual(parsed["f"], 3.5)
        self.assertIs(parsed["t"], True)
        self.assertIs(parsed["f2"], False)
        self.assertIsNone(parsed["n"])
        self.assertIsNone(parsed["n2"])
        self.assertEqual(parsed["s"], "plain text")

    def test_quoted_string_keeps_reserved_words(self):
        # The bug this caught in personas/time-starved-parent.yaml: a bare
        # `null` in never_uses became a YAML null and broke the tripwire.
        self.assertEqual(_fallback_loads('k: "null"\n'), {"k": "null"})
        self.assertEqual(_fallback_loads("k: 'true'\n"), {"k": "true"})

    def test_colon_inside_a_quoted_value(self):
        self.assertEqual(_fallback_loads('k: "a: b"\n'), {"k": "a: b"})

    def test_comments_are_stripped(self):
        parsed = _fallback_loads("# leading\na: 1  # trailing\nb: 2\n")
        self.assertEqual(parsed, {"a": 1, "b": 2})

    def test_hash_inside_a_quoted_value_is_not_a_comment(self):
        self.assertEqual(_fallback_loads('k: "a # b"\n'), {"k": "a # b"})

    def test_flow_collections(self):
        self.assertEqual(_fallback_loads("a: [1, 2, 3]\n"), {"a": [1, 2, 3]})
        self.assertEqual(_fallback_loads("a: {x: 1, y: two}\n"), {"a": {"x": 1, "y": "two"}})
        self.assertEqual(_fallback_loads("a: []\n"), {"a": []})

    def test_document_marker(self):
        self.assertEqual(_fallback_loads("---\na: 1\n"), {"a": 1})

    def test_literal_block_scalar(self):
        self.assertEqual(_fallback_loads("k: |-\n  one\n  two\n"), {"k": "one\ntwo"})

    def test_folded_block_scalar(self):
        self.assertEqual(_fallback_loads("k: >-\n  one\n  two\n"), {"k": "one two"})


class TestRejectsWhatItCannotHandle(unittest.TestCase):
    """Loud failure beats silent mis-parsing for anything outside the subset."""

    def test_anchors_rejected(self):
        with self.assertRaises(YamlError):
            _fallback_loads("a: &anchor value\nb: *anchor\n")

    def test_tags_rejected(self):
        with self.assertRaises(YamlError):
            _fallback_loads("a: !!python/object:os.system\n")

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(YamlError):
            _fallback_loads("a: 1\na: 2\n")

    def test_multiple_documents_rejected(self):
        with self.assertRaises(YamlError):
            _fallback_loads("---\na: 1\n---\nb: 2\n")

    def test_tab_indentation_rejected(self):
        with self.assertRaises(YamlError):
            _fallback_loads("a:\n\tb: 1\n")


class TestPublicApi(unittest.TestCase):
    def test_load_reports_the_filename(self):
        with self.assertRaises(YamlError) as caught:
            yamlite.load(KIT / "does-not-exist.yaml")
        self.assertIn("does-not-exist", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
