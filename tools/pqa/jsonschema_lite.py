"""A dependency-free JSON Schema validator (draft 2020-12 subset).

Why this exists instead of `pip install jsonschema`:

The review gate in this kit is meant to be a *constraint* — something the
pipeline cannot advance past. A gate that fails open because a Python package
was not installed on the machine running it is not a constraint, it is a
suggestion with extra steps. Every enforcement path here runs on a stock
Python 3 interpreter and nothing else.

The supported subset is deliberately small and covers exactly what the schemas
in `schemas/` use. Unknown keywords are ignored rather than rejected, so
schemas can carry annotations (`title`, `description`, `$comment`, `examples`)
without special handling. If you add a keyword to a schema, add it here too, or
it will silently not be enforced -- `pqa validate --self-check` guards against
that by failing on schema keywords this module does not implement.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "ValidationError",
    "SchemaError",
    "Validator",
    "validate",
    "load_schema",
    "SUPPORTED_KEYWORDS",
]

# Keywords this module actually enforces. Anything else in a schema is either an
# annotation (ignored on purpose) or an oversight -- see `unsupported_keywords`.
SUPPORTED_KEYWORDS = frozenset(
    {
        "$ref", "$defs", "type", "enum", "const",
        "properties", "required", "additionalProperties", "patternProperties",
        "minProperties", "maxProperties", "propertyNames",
        "items", "prefixItems", "minItems", "maxItems", "uniqueItems", "contains",
        "minLength", "maxLength", "pattern", "format",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
        "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
    }
)

_ANNOTATIONS = frozenset(
    {
        "$schema", "$id", "$comment", "$anchor",
        "title", "description", "examples", "default", "deprecated",
        "readOnly", "writeOnly", "unevaluatedProperties",
    }
)


@dataclass(frozen=True)
class ValidationError:
    """A single constraint violation, located by JSON Pointer."""

    path: str
    message: str
    keyword: str = ""

    def __str__(self) -> str:
        return f"{self.path or '<root>'}: {self.message}"


class SchemaError(Exception):
    """The schema itself is unusable (bad $ref, unreadable file, bad syntax)."""


# --------------------------------------------------------------------------
# type checks
# --------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    # bool is a subclass of int in Python; JSON Schema does not agree.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_matches(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return _is_int(value) or (isinstance(value, float) and value.is_integer())
    if name == "number":
        return _is_num(value)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type name: {name!r}")


def _canonical(value: Any) -> str:
    """Stable serialization, used for `enum`/`const`/`uniqueItems` comparison."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------
# format checks (only the ones the kit's schemas actually use)
# --------------------------------------------------------------------------

_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_URI = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

_FORMATS = {
    "date-time": lambda s: bool(_DATE_TIME.match(s)),
    "date": lambda s: bool(_DATE.match(s)),
    "uri": lambda s: bool(_URI.match(s)),
    "email": lambda s: "@" in s and " " not in s and len(s) >= 3,
}


# --------------------------------------------------------------------------
# $ref resolution
# --------------------------------------------------------------------------

class _Resolver:
    """Resolves `#/$defs/x` and `other-file.json#/$defs/x` against a base dir."""

    def __init__(self, root: dict, base_dir: Path | None) -> None:
        self._root = root
        self._base = base_dir
        self._files: dict[Path, dict] = {}

    def resolve(self, ref: str) -> tuple[dict, "_Resolver"]:
        file_part, _, pointer = ref.partition("#")
        if file_part:
            if self._base is None:
                raise SchemaError(f"cannot resolve file $ref {ref!r} without a base dir")
            target = (self._base / file_part).resolve()
            if target not in self._files:
                try:
                    self._files[target] = json.loads(target.read_text(encoding="utf-8"))
                except FileNotFoundError as exc:
                    raise SchemaError(f"$ref target not found: {target}") from exc
                except json.JSONDecodeError as exc:
                    raise SchemaError(f"$ref target is not valid JSON: {target}: {exc}") from exc
            doc = self._files[target]
            sub = _Resolver(doc, target.parent)
            sub._files = self._files
            return sub._follow(doc, pointer, ref), sub
        return self._follow(self._root, pointer, ref), self

    @staticmethod
    def _follow(doc: Any, pointer: str, ref: str) -> dict:
        node = doc
        for raw in pointer.split("/"):
            if raw in ("", "#"):
                continue
            token = raw.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                try:
                    node = node[int(token)]
                    continue
                except (ValueError, IndexError) as exc:
                    raise SchemaError(f"unresolvable $ref {ref!r}") from exc
            if not isinstance(node, dict) or token not in node:
                raise SchemaError(f"unresolvable $ref {ref!r}")
            node = node[token]
        if not isinstance(node, dict):
            raise SchemaError(f"$ref {ref!r} does not point at a schema object")
        return node


# --------------------------------------------------------------------------
# validator
# --------------------------------------------------------------------------

class Validator:
    """Validates instances against one schema. Reusable and stateless per call."""

    def __init__(self, schema: dict, base_dir: Path | None = None) -> None:
        if not isinstance(schema, dict):
            raise SchemaError("schema must be a JSON object")
        self.schema = schema
        self.base_dir = base_dir
        self._resolver = _Resolver(schema, base_dir)

    # -- public -----------------------------------------------------------

    def iter_errors(self, instance: Any) -> Iterator[ValidationError]:
        yield from self._validate(instance, self.schema, "", self._resolver)

    def validate(self, instance: Any) -> list[ValidationError]:
        return list(self.iter_errors(instance))

    def is_valid(self, instance: Any) -> bool:
        for _ in self.iter_errors(instance):
            return False
        return True

    def unsupported_keywords(self) -> set[str]:
        """Schema keywords present but not enforced -- silent no-ops.

        Used by `pqa validate --self-check` so that a schema cannot quietly stop
        constraining something after an edit.
        """
        found: set[str] = set()

        def walk(node: Any, in_properties: bool = False) -> None:
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if not isinstance(node, dict):
                return
            if not in_properties:
                for key in node:
                    if key not in SUPPORTED_KEYWORDS and key not in _ANNOTATIONS:
                        found.add(key)
            for key, value in node.items():
                # Under `properties`/`$defs`/`patternProperties` the keys are
                # names, not keywords; recurse one level deeper before judging.
                if key in ("properties", "$defs", "patternProperties"):
                    if isinstance(value, dict):
                        for sub in value.values():
                            walk(sub)
                elif key in SUPPORTED_KEYWORDS or key in _ANNOTATIONS:
                    walk(value)

        walk(self.schema)
        return found

    # -- core -------------------------------------------------------------

    def _validate(
        self, inst: Any, schema: Any, path: str, res: _Resolver
    ) -> Iterator[ValidationError]:
        if schema is True or schema == {}:
            return
        if schema is False:
            yield ValidationError(path, "no value is allowed here", "false")
            return
        if not isinstance(schema, dict):
            raise SchemaError(f"invalid schema at {path or '<root>'}: {schema!r}")

        if "$ref" in schema:
            target, sub_res = res.resolve(schema["$ref"])
            yield from self._validate(inst, target, path, sub_res)
            # Sibling keywords alongside $ref are valid in 2020-12; keep going.

        yield from self._check_type(inst, schema, path)
        yield from self._check_enum(inst, schema, path)
        yield from self._check_composition(inst, schema, path, res)

        if isinstance(inst, str):
            yield from self._check_string(inst, schema, path)
        if _is_num(inst):
            yield from self._check_number(inst, schema, path)
        if isinstance(inst, list):
            yield from self._check_array(inst, schema, path, res)
        if isinstance(inst, dict):
            yield from self._check_object(inst, schema, path, res)

    # -- keyword groups ---------------------------------------------------

    @staticmethod
    def _check_type(inst: Any, schema: dict, path: str) -> Iterator[ValidationError]:
        if "type" not in schema:
            return
        expected = schema["type"]
        names = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_matches(inst, n) for n in names):
            yield ValidationError(
                path,
                f"expected type {' or '.join(names)}, got {_json_type(inst)}",
                "type",
            )

    @staticmethod
    def _check_enum(inst: Any, schema: dict, path: str) -> Iterator[ValidationError]:
        if "enum" in schema:
            allowed = schema["enum"]
            if not any(_canonical(inst) == _canonical(a) for a in allowed):
                shown = ", ".join(repr(a) for a in allowed[:12])
                if len(allowed) > 12:
                    shown += ", ..."
                yield ValidationError(
                    path, f"{inst!r} is not one of [{shown}]", "enum"
                )
        if "const" in schema and _canonical(inst) != _canonical(schema["const"]):
            yield ValidationError(
                path, f"expected the constant {schema['const']!r}", "const"
            )

    def _check_composition(
        self, inst: Any, schema: dict, path: str, res: _Resolver
    ) -> Iterator[ValidationError]:
        for sub in schema.get("allOf", []):
            yield from self._validate(inst, sub, path, res)

        if "anyOf" in schema:
            branches = schema["anyOf"]
            if not any(self._passes(inst, s, path, res) for s in branches):
                yield ValidationError(
                    path, f"does not match any of the {len(branches)} allowed shapes", "anyOf"
                )

        if "oneOf" in schema:
            matched = [s for s in schema["oneOf"] if self._passes(inst, s, path, res)]
            if len(matched) != 1:
                got = "none" if not matched else f"{len(matched)}"
                yield ValidationError(
                    path,
                    f"must match exactly one of the {len(schema['oneOf'])} allowed shapes "
                    f"(matched {got})",
                    "oneOf",
                )

        if "not" in schema and self._passes(inst, schema["not"], path, res):
            yield ValidationError(path, "matches a forbidden shape", "not")

        if "if" in schema:
            if self._passes(inst, schema["if"], path, res):
                if "then" in schema:
                    yield from self._validate(inst, schema["then"], path, res)
            elif "else" in schema:
                yield from self._validate(inst, schema["else"], path, res)

    @staticmethod
    def _check_string(inst: str, schema: dict, path: str) -> Iterator[ValidationError]:
        if "minLength" in schema and len(inst) < schema["minLength"]:
            yield ValidationError(
                path, f"is shorter than the minimum {schema['minLength']} characters", "minLength"
            )
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            yield ValidationError(
                path, f"is longer than the maximum {schema['maxLength']} characters", "maxLength"
            )
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            yield ValidationError(
                path, f"does not match the pattern {schema['pattern']!r}", "pattern"
            )
        fmt = schema.get("format")
        if fmt in _FORMATS and not _FORMATS[fmt](inst):
            yield ValidationError(path, f"is not a valid {fmt}", "format")

    @staticmethod
    def _check_number(inst: Any, schema: dict, path: str) -> Iterator[ValidationError]:
        if "minimum" in schema and inst < schema["minimum"]:
            yield ValidationError(path, f"is less than the minimum {schema['minimum']}", "minimum")
        if "maximum" in schema and inst > schema["maximum"]:
            yield ValidationError(
                path, f"is greater than the maximum {schema['maximum']}", "maximum"
            )
        if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
            yield ValidationError(
                path, f"must be greater than {schema['exclusiveMinimum']}", "exclusiveMinimum"
            )
        if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
            yield ValidationError(
                path, f"must be less than {schema['exclusiveMaximum']}", "exclusiveMaximum"
            )
        if "multipleOf" in schema:
            step = schema["multipleOf"]
            if step > 0 and abs(inst / step - round(inst / step)) > 1e-9:
                yield ValidationError(path, f"is not a multiple of {step}", "multipleOf")

    def _check_array(
        self, inst: list, schema: dict, path: str, res: _Resolver
    ) -> Iterator[ValidationError]:
        if "minItems" in schema and len(inst) < schema["minItems"]:
            yield ValidationError(
                path, f"has {len(inst)} items, minimum is {schema['minItems']}", "minItems"
            )
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            yield ValidationError(
                path, f"has {len(inst)} items, maximum is {schema['maxItems']}", "maxItems"
            )
        if schema.get("uniqueItems"):
            seen: set[str] = set()
            for i, item in enumerate(inst):
                key = _canonical(item)
                if key in seen:
                    yield ValidationError(f"{path}/{i}", "is a duplicate item", "uniqueItems")
                seen.add(key)

        prefix = schema.get("prefixItems", [])
        for i, sub in enumerate(prefix):
            if i < len(inst):
                yield from self._validate(inst[i], sub, f"{path}/{i}", res)

        if "items" in schema:
            for i in range(len(prefix), len(inst)):
                yield from self._validate(inst[i], schema["items"], f"{path}/{i}", res)

        if "contains" in schema:
            if not any(self._passes(x, schema["contains"], path, res) for x in inst):
                yield ValidationError(path, "contains no item of the required shape", "contains")

    def _check_object(
        self, inst: dict, schema: dict, path: str, res: _Resolver
    ) -> Iterator[ValidationError]:
        for name in schema.get("required", []):
            if name not in inst:
                yield ValidationError(path, f"is missing the required property {name!r}", "required")

        if "minProperties" in schema and len(inst) < schema["minProperties"]:
            yield ValidationError(
                path, f"has {len(inst)} properties, minimum is {schema['minProperties']}",
                "minProperties",
            )
        if "maxProperties" in schema and len(inst) > schema["maxProperties"]:
            yield ValidationError(
                path, f"has {len(inst)} properties, maximum is {schema['maxProperties']}",
                "maxProperties",
            )

        props = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})

        for key, value in inst.items():
            child = f"{path}/{_escape(key)}"
            handled = False
            if key in props:
                handled = True
                yield from self._validate(value, props[key], child, res)
            for pattern, sub in patterns.items():
                if re.search(pattern, key):
                    handled = True
                    yield from self._validate(value, sub, child, res)
            if not handled and "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    yield ValidationError(
                        child, f"property {key!r} is not allowed here", "additionalProperties"
                    )
                else:
                    yield from self._validate(value, extra, child, res)
            if "propertyNames" in schema:
                yield from self._validate(key, schema["propertyNames"], child, res)

    # -- helpers ----------------------------------------------------------

    def _passes(self, inst: Any, schema: Any, path: str, res: _Resolver) -> bool:
        for _ in self._validate(inst, schema, path, res):
            return False
        return True


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_int(value):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------

def load_schema(path: str | Path) -> Validator:
    p = Path(path).resolve()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"schema not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise SchemaError(f"schema is not valid JSON: {p}: {exc}") from exc
    return Validator(doc, p.parent)


def validate(instance: Any, schema: dict | str | Path, base_dir: Path | None = None) -> list[ValidationError]:
    v = load_schema(schema) if isinstance(schema, (str, Path)) else Validator(schema, base_dir)
    return v.validate(instance)


def format_errors(errors: Iterable[ValidationError], limit: int = 25) -> str:
    items = list(errors)
    lines = [f"  - {e}" for e in items[:limit]]
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")
    return "\n".join(lines)
