"""YAML loading for persona cards, with no hard dependency on PyYAML.

Persona cards feed the drift check, and the drift check is enforcement. The same
argument that applies to `jsonschema_lite` applies here: an enforcement path that
only works when an optional package happens to be installed is not enforcement.

If PyYAML is importable it is used, because it is the reference implementation
and handles everything. If it is not, the fallback parser below covers the
subset that `schemas/persona.schema.json` permits:

    block mappings, block sequences, nested combinations of the two,
    plain / single-quoted / double-quoted scalars, `|` and `>` block scalars
    with `-` `+` chomping, flow sequences and mappings, `#` comments,
    int / float / bool / null scalars, and `---` document markers.

Not supported (and rejected loudly rather than silently mis-parsed): anchors,
aliases, tags, multiple documents, complex keys, and the YAML 1.1 `yes`/`no`
booleans. `tests/test_yamlite.py` asserts that both parsers agree on every card
in `personas/`, so the fallback cannot drift away from the reference.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .core import PqaError

__all__ = ["load", "loads", "YamlError", "USING_PYYAML"]


class YamlError(PqaError):
    """The document is malformed, or uses a feature outside the supported subset.

    A PqaError, so a malformed card or scenario is reported like any other bad
    input -- listed as INVALID, or exit 1 -- rather than as a traceback.
    """


try:  # pragma: no cover - environment dependent
    import yaml as _pyyaml

    USING_PYYAML = True
except ImportError:  # pragma: no cover - environment dependent
    _pyyaml = None
    USING_PYYAML = False


def loads(text: str) -> Any:
    """Parse a YAML document from a string."""
    if _pyyaml is not None:
        try:
            return _pyyaml.safe_load(text)
        except _pyyaml.YAMLError as exc:
            raise YamlError(str(exc)) from exc
    return _fallback_loads(text)


def load(path: str | Path) -> Any:
    """Parse a YAML document from a file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise YamlError(f"{p}: {exc.strerror or exc}") from exc
    try:
        return loads(text)
    except YamlError as exc:
        raise YamlError(f"{p}: {exc}") from exc


# ==========================================================================
# fallback parser
# ==========================================================================

# Anchors (&x), aliases (*x) and tags (!x, !!x). Matched on the introducer
# rather than the whole token, so `!!python/object:os.system` is caught despite
# the trailing colon. This can false-positive on prose like "the *important*
# bit"; that is the intended direction to err in, since the result is a loud
# error the card author fixes by quoting, rather than a silent mis-parse.
_UNSUPPORTED = re.compile(r"(?:^|\s)(?:[&*][A-Za-z0-9_\-]+|!!?[A-Za-z0-9_\-/]+)")
_BLOCK_HEADER = re.compile(r"(?:^|\s)([|>])([+-]?)\s*$")


class _Line:
    __slots__ = ("indent", "content", "no", "literal")

    def __init__(self, indent: int, content: str, no: int, literal: str | None = None) -> None:
        self.indent = indent
        self.content = content
        self.no = no
        self.literal = literal

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Line({self.indent}, {self.content!r}, line={self.no})"


def _fallback_loads(text: str) -> Any:
    lines = _expand(_scan(text))
    if not lines:
        return None
    value, index = _parse_node(lines, 0, lines[0].indent)
    if index < len(lines):
        raise YamlError(f"line {lines[index].no}: unexpected indentation")
    return value


# -- scanning --------------------------------------------------------------

def _scan(text: str) -> list[_Line]:
    raw = text.splitlines()
    lines: list[_Line] = []
    i = 0
    doc_count = 0

    while i < len(raw):
        source = raw[i]
        stripped = source.strip()

        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if stripped in ("---", "..."):
            if stripped == "---":
                doc_count += 1
                if doc_count > 1:
                    raise YamlError(f"line {i + 1}: multiple documents are not supported")
            i += 1
            continue

        leading = source[: len(source) - len(source.lstrip())]
        if "\t" in leading:
            raise YamlError(f"line {i + 1}: tabs cannot be used for indentation")
        indent = len(leading)

        content = _strip_comment(stripped)
        if _UNSUPPORTED.search(" " + content):
            raise YamlError(f"line {i + 1}: anchors, aliases and tags are not supported")

        header = _BLOCK_HEADER.search(content)
        if header:
            style, chomp = header.group(1), header.group(2)
            head = content[: header.start()].rstrip()
            body, i = _read_block_body(raw, i + 1, indent)
            lines.append(_Line(indent, head, i, _fold(body, style, chomp)))
            continue

        lines.append(_Line(indent, content, i + 1))
        i += 1

    return lines


def _read_block_body(raw: list[str], i: int, parent_indent: int) -> tuple[list[str], int]:
    body: list[str] = []
    base: int | None = None
    while i < len(raw):
        line = raw[i]
        if not line.strip():
            body.append("")
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            break
        if base is None:
            base = indent
        body.append(line[base:] if len(line) >= base else line.lstrip(" "))
        i += 1
    return body, i


def _fold(body: list[str], style: str, chomp: str) -> str:
    trailing = 0
    while body and body[-1] == "":
        body.pop()
        trailing += 1

    if style == "|":
        core = "\n".join(body)
    else:
        # `>` joins consecutive non-blank lines with a space; a blank line is a
        # hard break.
        core = ""
        pending_break = False
        for line in body:
            if line == "":
                core += "\n"
                pending_break = True
                continue
            if core and not pending_break:
                core += " "
            core += line
            pending_break = False

    if chomp == "-":
        return core
    if chomp == "+":
        return core + "\n" * (trailing + 1) if core or trailing else ""
    return core + "\n" if core else ""


def _strip_comment(text: str) -> str:
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


# -- sequence expansion ----------------------------------------------------

def _expand(lines: list[_Line]) -> list[_Line]:
    """Rewrite `- item` into a bare `-` marker plus the item at its own column.

    This makes every node uniform: the parser only ever sees marker lines and
    plain lines, never a hybrid.
    """
    out: list[_Line] = []
    for line in lines:
        indent, content, literal = line.indent, line.content, line.literal
        while content.startswith("-") and (len(content) == 1 or content[1] == " "):
            out.append(_Line(indent, "-", line.no))
            rest = content[1:]
            indent = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
            content = rest.strip()
        if content or literal is not None:
            out.append(_Line(indent, content, line.no, literal))
    return out


# -- parsing ---------------------------------------------------------------

def _parse_node(lines: list[_Line], i: int, indent: int) -> tuple[Any, int]:
    line = lines[i]
    if line.content == "-" and line.literal is None:
        return _parse_sequence(lines, i, indent)
    key, _ = _split_key(line.content)
    if key is None:
        if line.literal is not None:
            return line.literal, i + 1
        return _scalar(line.content, line.no), i + 1
    return _parse_mapping(lines, i, indent)


def _parse_sequence(lines: list[_Line], i: int, indent: int) -> tuple[list, int]:
    out: list[Any] = []
    while i < len(lines) and lines[i].indent == indent and lines[i].content == "-":
        i += 1
        if i < len(lines) and lines[i].indent > indent:
            value, i = _parse_node(lines, i, lines[i].indent)
            out.append(value)
        else:
            out.append(None)
    return out, i


def _parse_mapping(lines: list[_Line], i: int, indent: int) -> tuple[dict, int]:
    out: dict[str, Any] = {}
    while i < len(lines) and lines[i].indent == indent and lines[i].content != "-":
        line = lines[i]
        key, rest = _split_key(line.content)
        if key is None:
            raise YamlError(f"line {line.no}: expected 'key: value', got {line.content!r}")
        if key in out:
            raise YamlError(f"line {line.no}: duplicate key {key!r}")

        if line.literal is not None:
            out[key] = line.literal
            i += 1
            continue

        if rest:
            out[key] = _scalar(rest, line.no)
            i += 1
            continue

        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if nxt is not None and (
            nxt.indent > indent or (nxt.indent == indent and nxt.content == "-")
        ):
            out[key], i = _parse_node(lines, i + 1, nxt.indent)
        else:
            out[key] = None
            i += 1
    return out, i


def _split_key(text: str) -> tuple[str | None, str]:
    if not text or text[0] in "[{":
        return None, ""
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0 and (i + 1 == len(text) or text[i + 1] == " "):
            return _unquote(text[:i].strip()), text[i + 1 :].strip()
        i += 1
    return None, ""


# -- scalars ---------------------------------------------------------------

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/"}


def _scalar(text: str, line_no: int = 0) -> Any:
    text = text.strip()
    if not text or text in ("null", "Null", "NULL", "~"):
        return None
    if text[0] in "[{":
        return _parse_flow(text, line_no)
    if text[0] in "\"'":
        return _unquote(text)
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text) and not _INT.match(text):
        return float(text)
    return text


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if len(text) >= 2 and text[0] == text[-1] == '"':
        body = text[1:-1]
        out: list[str] = []
        i = 0
        while i < len(body):
            if body[i] == "\\" and i + 1 < len(body):
                out.append(_ESCAPES.get(body[i + 1], body[i + 1]))
                i += 2
                continue
            out.append(body[i])
            i += 1
        return "".join(out)
    return text


def _parse_flow(text: str, line_no: int) -> Any:
    value, index = _flow_node(text, 0, line_no)
    if text[index:].strip():
        raise YamlError(f"line {line_no}: trailing content after flow collection")
    return value


def _flow_node(text: str, i: int, line_no: int) -> tuple[Any, int]:
    while i < len(text) and text[i] == " ":
        i += 1
    if i >= len(text):
        raise YamlError(f"line {line_no}: unexpected end of flow collection")
    if text[i] == "[":
        return _flow_collection(text, i, line_no, "]", is_map=False)
    if text[i] == "{":
        return _flow_collection(text, i, line_no, "}", is_map=True)
    return _flow_scalar(text, i, line_no)


def _flow_collection(
    text: str, i: int, line_no: int, closer: str, is_map: bool
) -> tuple[Any, int]:
    i += 1
    items: list[Any] = []
    pairs: dict[str, Any] = {}
    while True:
        while i < len(text) and text[i] in " ,":
            i += 1
        if i >= len(text):
            raise YamlError(f"line {line_no}: unterminated flow collection")
        if text[i] == closer:
            return (pairs if is_map else items), i + 1
        if is_map:
            key, i = _flow_scalar(text, i, line_no, stop=":")
            while i < len(text) and text[i] in " :":
                i += 1
            value, i = _flow_node(text, i, line_no)
            pairs[str(key)] = value
        else:
            value, i = _flow_node(text, i, line_no)
            items.append(value)


def _flow_scalar(text: str, i: int, line_no: int, stop: str = "") -> tuple[Any, int]:
    if text[i] in "\"'":
        quote = text[i]
        j = i + 1
        while j < len(text):
            if quote == '"' and text[j] == "\\":
                j += 2
                continue
            if text[j] == quote:
                if quote == "'" and j + 1 < len(text) and text[j + 1] == "'":
                    j += 2
                    continue
                return _unquote(text[i : j + 1]), j + 1
            j += 1
        raise YamlError(f"line {line_no}: unterminated quoted scalar")
    j = i
    while j < len(text) and text[j] not in ",]}" + stop:
        j += 1
    return _scalar(text[i:j], line_no), j
