"""The validator is enforcement, so its own gaps are the kit's gaps."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import KIT  # noqa: F401  (sets sys.path)

from pqa.jsonschema_lite import SchemaError, Validator, load_schema


def valid(schema, instance) -> bool:
    return Validator(schema).is_valid(instance)


class TestTypes(unittest.TestCase):
    def test_basic_types(self):
        self.assertTrue(valid({"type": "string"}, "x"))
        self.assertFalse(valid({"type": "string"}, 1))
        self.assertTrue(valid({"type": "array"}, []))
        self.assertTrue(valid({"type": "null"}, None))

    def test_boolean_is_not_an_integer(self):
        # bool subclasses int in Python; JSON Schema disagrees, and conflating
        # them would let `true` satisfy a step number.
        self.assertFalse(valid({"type": "integer"}, True))
        self.assertFalse(valid({"type": "number"}, False))
        self.assertTrue(valid({"type": "boolean"}, True))

    def test_integer_accepts_integral_float(self):
        self.assertTrue(valid({"type": "integer"}, 5.0))
        self.assertFalse(valid({"type": "integer"}, 5.5))

    def test_union_type(self):
        self.assertTrue(valid({"type": ["string", "null"]}, None))
        self.assertFalse(valid({"type": ["string", "null"]}, 3))


class TestObjects(unittest.TestCase):
    def test_required(self):
        schema = {"type": "object", "required": ["a"]}
        self.assertTrue(valid(schema, {"a": 1}))
        self.assertFalse(valid(schema, {"b": 1}))

    def test_additional_properties_false(self):
        schema = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
        self.assertTrue(valid(schema, {"a": 1}))
        self.assertFalse(valid(schema, {"a": 1, "severity": "minor"}))

    def test_error_path_is_a_json_pointer(self):
        schema = {"type": "object", "properties": {"a": {"type": "object",
                  "properties": {"b": {"type": "string"}}}}}
        errors = Validator(schema).validate({"a": {"b": 1}})
        self.assertEqual(errors[0].path, "/a/b")

    def test_pattern_properties(self):
        schema = {"type": "object", "patternProperties": {"^x": {"type": "integer"}}}
        self.assertTrue(valid(schema, {"xa": 1}))
        self.assertFalse(valid(schema, {"xa": "no"}))

    def test_property_names(self):
        schema = {"type": "object", "propertyNames": {"pattern": "^[a-z]+$"}}
        self.assertTrue(valid(schema, {"abc": 1}))
        self.assertFalse(valid(schema, {"ABC": 1}))


class TestArrays(unittest.TestCase):
    def test_items_and_bounds(self):
        schema = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 2}
        self.assertTrue(valid(schema, [1]))
        self.assertFalse(valid(schema, []))
        self.assertFalse(valid(schema, [1, 2, 3]))
        self.assertFalse(valid(schema, ["x"]))

    def test_unique_items(self):
        schema = {"type": "array", "uniqueItems": True}
        self.assertTrue(valid(schema, [1, 2]))
        self.assertFalse(valid(schema, [1, 1]))
        self.assertFalse(valid(schema, [{"a": 1}, {"a": 1}]))

    def test_prefix_items(self):
        schema = {"type": "array", "prefixItems": [{"type": "string"}, {"type": "integer"}]}
        self.assertTrue(valid(schema, ["a", 1]))
        self.assertFalse(valid(schema, [1, 1]))

    def test_contains(self):
        schema = {"type": "array", "contains": {"const": "x"}}
        self.assertTrue(valid(schema, ["a", "x"]))
        self.assertFalse(valid(schema, ["a", "b"]))


class TestScalarConstraints(unittest.TestCase):
    def test_string_bounds_and_pattern(self):
        schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^a"}
        self.assertTrue(valid(schema, "abc"))
        self.assertFalse(valid(schema, "a"))
        self.assertFalse(valid(schema, "abcde"))
        self.assertFalse(valid(schema, "bcd"))

    def test_numeric_bounds(self):
        schema = {"type": "integer", "minimum": 1, "maximum": 10}
        self.assertTrue(valid(schema, 5))
        self.assertFalse(valid(schema, 0))
        self.assertFalse(valid(schema, 11))

    def test_exclusive_bounds(self):
        self.assertFalse(valid({"exclusiveMinimum": 0}, 0))
        self.assertTrue(valid({"exclusiveMinimum": 0}, 1))

    def test_multiple_of(self):
        self.assertTrue(valid({"multipleOf": 0.5}, 1.5))
        self.assertFalse(valid({"multipleOf": 0.5}, 1.7))

    def test_enum_and_const(self):
        self.assertTrue(valid({"enum": ["a", "b"]}, "a"))
        self.assertFalse(valid({"enum": ["a", "b"]}, "c"))
        self.assertTrue(valid({"const": "qa-agent"}, "qa-agent"))
        self.assertFalse(valid({"const": "qa-agent"}, "user-agent"))

    def test_date_time_format(self):
        schema = {"type": "string", "format": "date-time"}
        self.assertTrue(valid(schema, "2026-08-15T09:12:04Z"))
        self.assertTrue(valid(schema, "2026-08-15T09:12:04+01:00"))
        self.assertFalse(valid(schema, "2026-08-15"))
        self.assertFalse(valid(schema, "yesterday"))


class TestComposition(unittest.TestCase):
    def test_one_of_requires_exactly_one(self):
        schema = {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertTrue(valid(schema, "a"))
        self.assertTrue(valid(schema, 1))
        self.assertFalse(valid(schema, None))

    def test_one_of_rejects_ambiguous_match(self):
        schema = {"oneOf": [{"minimum": 0}, {"maximum": 10}]}
        self.assertFalse(valid(schema, 5))

    def test_any_of(self):
        schema = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        self.assertTrue(valid(schema, 1))
        self.assertFalse(valid(schema, []))

    def test_all_of(self):
        schema = {"allOf": [{"type": "string"}, {"minLength": 3}]}
        self.assertTrue(valid(schema, "abc"))
        self.assertFalse(valid(schema, "ab"))

    def test_not(self):
        self.assertFalse(valid({"not": {"type": "string"}}, "a"))
        self.assertTrue(valid({"not": {"type": "string"}}, 1))

    def test_if_then_else(self):
        # This is the shape findings.reviewed uses for conditional requirements.
        schema = {
            "if": {"properties": {"verdict": {"const": "rejected"}}, "required": ["verdict"]},
            "then": {"required": ["rejection_reason"]},
        }
        self.assertTrue(valid(schema, {"verdict": "accepted"}))
        self.assertTrue(valid(schema, {"verdict": "rejected", "rejection_reason": "no_evidence"}))
        self.assertFalse(valid(schema, {"verdict": "rejected"}))


class TestRefs(unittest.TestCase):
    def test_local_ref(self):
        schema = {"$defs": {"s": {"type": "string"}}, "$ref": "#/$defs/s"}
        self.assertTrue(valid(schema, "a"))
        self.assertFalse(valid(schema, 1))

    def test_file_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "other.json").write_text(json.dumps({"$defs": {"s": {"type": "integer"}}}))
            (root / "main.json").write_text(json.dumps({"$ref": "other.json#/$defs/s"}))
            validator = load_schema(root / "main.json")
            self.assertTrue(validator.is_valid(1))
            self.assertFalse(validator.is_valid("x"))

    def test_unresolvable_ref_raises(self):
        with self.assertRaises(SchemaError):
            Validator({"$ref": "#/$defs/nope"}).validate("x")

    def test_missing_schema_file_raises(self):
        with self.assertRaises(SchemaError):
            load_schema("/nonexistent/schema.json")


class TestUnsupportedKeywordDetection(unittest.TestCase):
    """A schema keyword we do not implement is a constraint that is not applied."""

    def test_flags_unimplemented_keyword(self):
        validator = Validator({"type": "object", "dependentRequired": {"a": ["b"]}})
        self.assertIn("dependentRequired", validator.unsupported_keywords())

    def test_annotations_are_not_flagged(self):
        validator = Validator({"title": "x", "description": "y", "$comment": "z", "examples": []})
        self.assertEqual(validator.unsupported_keywords(), set())

    def test_property_names_are_not_mistaken_for_keywords(self):
        validator = Validator({"properties": {"title": {"type": "string"},
                                              "anythingGoes": {"type": "string"}}})
        self.assertEqual(validator.unsupported_keywords(), set())

    def test_shipped_schemas_are_fully_enforced(self):
        for path in sorted((KIT / "schemas").glob("*.schema.json")):
            with self.subTest(schema=path.name):
                self.assertEqual(load_schema(path).unsupported_keywords(), set())


if __name__ == "__main__":
    unittest.main()
